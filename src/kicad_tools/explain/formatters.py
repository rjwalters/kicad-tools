"""Output formatters for explanation results.

This module provides different formatting options for explanation results,
including plain text, JSON, and markdown.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ExplainedViolation, ExplanationResult


def format_text(result: ExplanationResult) -> str:
    """Format an explanation result as plain text.

    Args:
        result: The explanation result to format

    Returns:
        Formatted text string
    """
    lines = []

    # Title and rule
    lines.append(f"Rule: {result.rule}")
    lines.append(f"Title: {result.title}")
    lines.append(f"Severity: {result.severity}")
    lines.append("")

    # Explanation
    lines.append("Explanation:")
    for line in result.explanation.split("\n"):
        lines.append(f"  {line.strip()}")
    lines.append("")

    # Spec reference
    if result.spec_reference:
        lines.append("Specification Reference:")
        lines.append(f"  Name: {result.spec_reference.name}")
        if result.spec_reference.section:
            lines.append(f"  Section: {result.spec_reference.section}")
        if result.spec_reference.url:
            lines.append(f"  URL: {result.spec_reference.url}")
        if result.spec_reference.version:
            lines.append(f"  Version: {result.spec_reference.version}")
        lines.append("")

    # Values
    if result.current_value is not None or result.required_value is not None:
        lines.append("Values:")
        if result.current_value is not None:
            lines.append(f"  Current: {result.current_value}{result.unit}")
        if result.required_value is not None:
            lines.append(f"  Required: {result.required_value}{result.unit}")
        lines.append("")

    # Fix suggestions
    if result.fix_suggestions:
        lines.append("Fix Suggestions:")
        for i, suggestion in enumerate(result.fix_suggestions, 1):
            lines.append(f"  {i}. {suggestion}")
        lines.append("")

    # Related rules
    if result.related_rules:
        lines.append(f"Related Rules: {', '.join(result.related_rules)}")

    return "\n".join(lines)


def format_tree(result: ExplanationResult) -> str:
    """Format an explanation result as a tree structure.

    This format is more compact and suitable for inline display with violations.

    Args:
        result: The explanation result to format

    Returns:
        Formatted tree string
    """
    return result.format_tree()


def format_json(result: ExplanationResult, indent: int = 2) -> str:
    """Format an explanation result as JSON.

    Args:
        result: The explanation result to format
        indent: Number of spaces for indentation

    Returns:
        JSON string
    """
    return json.dumps(result.to_dict(), indent=indent)


def format_markdown(result: ExplanationResult) -> str:
    """Format an explanation result as markdown.

    Args:
        result: The explanation result to format

    Returns:
        Markdown string
    """
    lines = []

    # Title
    lines.append(f"## {result.title}")
    lines.append("")
    lines.append(f"**Rule ID:** `{result.rule}`  ")
    lines.append(f"**Severity:** {result.severity}")
    lines.append("")

    # Explanation
    lines.append("### Explanation")
    lines.append("")
    lines.append(result.explanation)
    lines.append("")

    # Spec reference
    if result.spec_reference:
        lines.append("### Specification Reference")
        lines.append("")
        ref = result.spec_reference
        if ref.url:
            lines.append(f"- **Source:** [{ref.name}]({ref.url})")
        else:
            lines.append(f"- **Source:** {ref.name}")
        if ref.section:
            lines.append(f"- **Section:** {ref.section}")
        if ref.version:
            lines.append(f"- **Version:** {ref.version}")
        lines.append("")

    # Values
    if result.current_value is not None or result.required_value is not None:
        lines.append("### Values")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        if result.current_value is not None:
            lines.append(f"| Current | {result.current_value}{result.unit} |")
        if result.required_value is not None:
            lines.append(f"| Required | {result.required_value}{result.unit} |")
        lines.append("")

    # Fix suggestions
    if result.fix_suggestions:
        lines.append("### Fix Suggestions")
        lines.append("")
        for suggestion in result.fix_suggestions:
            lines.append(f"- {suggestion}")
        lines.append("")

    # Related rules
    if result.related_rules:
        lines.append("### Related Rules")
        lines.append("")
        for rule in result.related_rules:
            lines.append(f"- `{rule}`")
        lines.append("")

    return "\n".join(lines)


def format_violations_text(violations: list[ExplainedViolation]) -> str:
    """Format a list of explained violations as plain text.

    Args:
        violations: List of explained violations

    Returns:
        Formatted text string
    """
    lines = []

    for i, ev in enumerate(violations, 1):
        v = ev.violation
        exp = ev.explanation

        lines.append(f"[{i}] {v.type_str}: {v.message}")
        lines.append(f"    ├─ Rule: {exp.title}")

        if exp.spec_reference:
            lines.append(f"    ├─ Spec: {exp.spec_reference.name}")

        if hasattr(v, "primary_location") and v.primary_location:
            loc = v.primary_location
            lines.append(f"    ├─ Location: ({loc.x_mm:.2f}, {loc.y_mm:.2f}) mm")

        if exp.fix_suggestions:
            lines.append(f"    └─ Fix: {exp.fix_suggestions[0]}")
        else:
            lines.append("    └─ See rule explanation for guidance")

        lines.append("")

    return "\n".join(lines)


def format_violations_json(violations: list[ExplainedViolation], indent: int = 2) -> str:
    """Format a list of explained violations as JSON.

    Args:
        violations: List of explained violations
        indent: Number of spaces for indentation

    Returns:
        JSON string
    """
    data = [ev.to_dict() for ev in violations]
    return json.dumps(data, indent=indent)


def format_violations_markdown(violations: list[ExplainedViolation]) -> str:
    """Format a list of explained violations as markdown.

    Args:
        violations: List of explained violations

    Returns:
        Markdown string
    """
    lines = []
    lines.append("# DRC Violations with Explanations")
    lines.append("")
    lines.append(f"**Total violations:** {len(violations)}")
    lines.append("")

    for i, ev in enumerate(violations, 1):
        v = ev.violation
        exp = ev.explanation

        lines.append(f"## {i}. {v.type_str}")
        lines.append("")
        lines.append(f"**Message:** {v.message}")
        lines.append("")

        if hasattr(v, "primary_location") and v.primary_location:
            loc = v.primary_location
            lines.append(f"**Location:** ({loc.x_mm:.2f}, {loc.y_mm:.2f}) mm")
            lines.append("")

        lines.append(f"### Explanation: {exp.title}")
        lines.append("")
        lines.append(exp.explanation)
        lines.append("")

        if exp.spec_reference:
            ref = exp.spec_reference
            if ref.url:
                lines.append(f"**Spec:** [{ref.name}]({ref.url})")
            else:
                lines.append(f"**Spec:** {ref.name}")
            lines.append("")

        if exp.fix_suggestions:
            lines.append("**Fix suggestions:**")
            for suggestion in exp.fix_suggestions:
                lines.append(f"- {suggestion}")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


# Format function registry
FORMATTERS = {
    "text": format_text,
    "tree": format_tree,
    "json": format_json,
    "markdown": format_markdown,
    "md": format_markdown,
}

VIOLATION_FORMATTERS = {
    "text": format_violations_text,
    "json": format_violations_json,
    "markdown": format_violations_markdown,
    "md": format_violations_markdown,
}


def format_result(
    result: ExplanationResult,
    format_type: str = "text",
) -> str:
    """Format an explanation result using the specified format.

    Args:
        result: The explanation result to format
        format_type: Format type ("text", "tree", "json", "markdown", "md")

    Returns:
        Formatted string

    Raises:
        ValueError: If format_type is not recognized
    """
    formatter = FORMATTERS.get(format_type)
    if not formatter:
        available = ", ".join(FORMATTERS.keys())
        raise ValueError(f"Unknown format: {format_type!r}. Available: {available}")
    return formatter(result)


def format_violations(
    violations: list[ExplainedViolation],
    format_type: str = "text",
) -> str:
    """Format explained violations using the specified format.

    Args:
        violations: List of explained violations
        format_type: Format type ("text", "json", "markdown", "md")

    Returns:
        Formatted string

    Raises:
        ValueError: If format_type is not recognized
    """
    formatter = VIOLATION_FORMATTERS.get(format_type)
    if not formatter:
        available = ", ".join(VIOLATION_FORMATTERS.keys())
        raise ValueError(f"Unknown format: {format_type!r}. Available: {available}")
    return formatter(violations)

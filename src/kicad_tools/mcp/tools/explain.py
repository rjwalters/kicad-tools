"""MCP tool for explaining design rules and DRC violations.

Provides the explain_rule function for AI agents to get detailed
explanations of design rules with spec references and fix suggestions.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from kicad_tools.explain import (
    ExplanationResult,
    explain,
    explain_net_constraints,
    explain_violations,
    list_rules,
    search_rules,
)
from kicad_tools.explain.models import ExplainedViolation

logger = logging.getLogger(__name__)


def explain_rule(
    rule_id: str,
    current_value: float | None = None,
    required_value: float | None = None,
    unit: str = "mm",
    net1: str | None = None,
    net2: str | None = None,
) -> dict[str, Any]:
    """Explain a design rule with spec references and fix suggestions.

    This tool provides detailed explanations for design rules, including
    references to manufacturer specifications and interface standards,
    along with actionable fix suggestions.

    Args:
        rule_id: The rule to explain (e.g., "trace_clearance", "via_drill",
                 "usb_20_high_speed_differential_impedance")
        current_value: Current/actual measured value (optional)
        required_value: Required/minimum value (optional)
        unit: Unit of measurement (default: "mm")
        net1: First net name for context (optional)
        net2: Second net name for context (optional)

    Returns:
        Dictionary with rule explanation, spec reference, and fix suggestions:
        {
            "rule": "trace_clearance",
            "title": "Minimum Trace Clearance",
            "explanation": "Manufacturing process limits prevent...",
            "spec_reference": {
                "name": "JLCPCB Manufacturing Capabilities",
                "section": "PCB Specifications > Minimum Clearance",
                "url": "https://jlcpcb.com/capabilities/pcb-capabilities",
                "version": "2024-01"
            },
            "current_value": 0.15,
            "required_value": 0.2,
            "unit": "mm",
            "severity": "error",
            "fix_suggestions": [
                "Increase spacing by at least 0.05mm to meet minimum clearance"
            ],
            "related_rules": ["trace_width", "via_clearance"]
        }

    Raises:
        ValueError: If the rule_id is not found in the registry

    Example:
        >>> result = explain_rule("trace_clearance", current_value=0.15, required_value=0.2)
        >>> print(result["fix_suggestions"][0])
        "Increase spacing by at least 0.05mm to meet minimum clearance"
    """
    context: dict[str, Any] = {"unit": unit}

    if current_value is not None:
        context["value"] = current_value
    if required_value is not None:
        context["required_value"] = required_value
    if net1:
        context["net1"] = net1
    if net2:
        context["net2"] = net2

    try:
        result = explain(rule_id, context if context else None)
        return result.to_dict()
    except ValueError as e:
        # Return error information
        return {
            "error": str(e),
            "available_rules": list_rules()[:20],  # First 20 rules
            "hint": "Use list_available_rules() to see all available rules",
        }


def explain_net(
    net_name: str,
    interface_type: str | None = None,
) -> dict[str, Any]:
    """Explain why a net has certain constraints.

    This tool explains the constraints applied to a net based on its
    interface type (USB, SPI, I2C, etc.) or inferred from its name.

    Args:
        net_name: Name of the net (e.g., "USB_D+", "SCL", "MISO")
        interface_type: Optional explicit interface type (usb, i2c, spi)

    Returns:
        Dictionary explaining the net's constraints:
        {
            "rule": "usb_20_high_speed_constraints",
            "title": "USB 2.0 High Speed Constraints",
            "explanation": "Net 'USB_D+' is part of a USB 2.0 HS interface...",
            "spec_reference": {...},
            "severity": "info"
        }

    Example:
        >>> result = explain_net("USB_D+")
        >>> print(result["explanation"])
        "Net 'USB_D+' is part of a USB 2.0 High Speed interface..."
    """
    result = explain_net_constraints(net_name, interface_type)
    return result.to_dict()


def list_available_rules() -> dict[str, Any]:
    """List all available rule IDs that can be explained.

    Returns:
        Dictionary with available rules organized by category:
        {
            "total": 25,
            "rules": ["trace_clearance", "trace_width", ...],
            "categories": {
                "manufacturer": ["trace_clearance", "via_drill", ...],
                "interface": ["usb_20_high_speed_differential_impedance", ...]
            }
        }
    """
    all_rules = list_rules()

    # Categorize rules
    manufacturer_rules = [r for r in all_rules if not any(
        x in r.lower() for x in ["usb", "i2c", "spi", "uart", "jtag"]
    )]
    interface_rules = [r for r in all_rules if r not in manufacturer_rules]

    return {
        "total": len(all_rules),
        "rules": all_rules,
        "categories": {
            "manufacturer": manufacturer_rules,
            "interface": interface_rules,
        },
    }


def search_available_rules(query: str) -> dict[str, Any]:
    """Search for rules matching a query string.

    Args:
        query: Search term to match against rule IDs and titles

    Returns:
        Dictionary with matching rules:
        {
            "query": "clearance",
            "matches": [
                {
                    "rule_id": "trace_clearance",
                    "title": "Minimum Trace Clearance",
                    "severity": "error"
                },
                ...
            ],
            "total": 3
        }
    """
    matches = search_rules(query)

    return {
        "query": query,
        "matches": [
            {
                "rule_id": exp.rule_id,
                "title": exp.title,
                "severity": exp.severity,
            }
            for exp in matches
        ],
        "total": len(matches),
    }


def explain_drc_violations(
    violations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Explain a list of DRC violations with spec references.

    Takes violation data and returns explanations for each violation type.
    This is useful for batch-processing DRC results.

    Args:
        violations: List of violation dictionaries with at least:
            - type or rule_id: The violation type
            - message: Violation message
            Optionally:
            - required_value_mm: Required value
            - actual_value_mm: Actual measured value
            - nets: List of involved net names
            - location: (x, y) tuple or dict with x_mm, y_mm

    Returns:
        List of explained violations with original data plus explanations:
        [
            {
                "violation": {...original violation data...},
                "explanation": {
                    "rule": "trace_clearance",
                    "title": "Minimum Trace Clearance",
                    "explanation": "...",
                    "spec_reference": {...},
                    "fix_suggestions": [...]
                }
            },
            ...
        ]

    Example:
        >>> violations = [
        ...     {"type": "clearance", "message": "Clearance violation"},
        ...     {"type": "trace_width", "message": "Track too narrow"}
        ... ]
        >>> explained = explain_drc_violations(violations)
    """
    # Convert dict violations to a simple object for the explain function
    class ViolationAdapter:
        def __init__(self, data: dict[str, Any]):
            self._data = data

        @property
        def type_str(self) -> str:
            return self._data.get("type") or self._data.get("rule_id", "unknown")

        @property
        def type(self):
            return self.type_str

        @property
        def message(self) -> str:
            return self._data.get("message", "")

        @property
        def required_value_mm(self) -> float | None:
            return self._data.get("required_value_mm")

        @property
        def actual_value_mm(self) -> float | None:
            return self._data.get("actual_value_mm")

        @property
        def nets(self) -> list[str]:
            return self._data.get("nets", [])

        @property
        def primary_location(self):
            loc = self._data.get("location")
            if not loc:
                return None
            if isinstance(loc, dict):
                class Loc:
                    x_mm = loc.get("x_mm", 0)
                    y_mm = loc.get("y_mm", 0)
                return Loc()
            return None

        @property
        def severity(self) -> str:
            return self._data.get("severity", "error")

        def to_dict(self) -> dict[str, Any]:
            return self._data

    adapted = [ViolationAdapter(v) for v in violations]
    explained = explain_violations(adapted)

    return [ev.to_dict() for ev in explained]

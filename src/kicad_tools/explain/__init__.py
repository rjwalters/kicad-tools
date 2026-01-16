"""Queryable explanation system with spec references.

This module provides tools for explaining DRC violations and design rule
constraints with references to specifications and fix suggestions.

Example:
    >>> from kicad_tools.explain import explain
    >>> result = explain("trace_clearance",
    ...                  context={"net1": "USB_D+", "net2": "GND", "value": 0.15})
    >>> print(result.title)
    Minimum Trace Clearance
    >>> print(result.fix_suggestions[0])
    Increase spacing by at least 0.05mm to meet minimum clearance

With DRC violations:
    >>> from kicad_tools.explain import explain_violations
    >>> from kicad_tools.drc import DRCReport
    >>> report = DRCReport.load("design-drc.rpt")
    >>> explained = explain_violations(report.violations)
    >>> for v in explained:
    ...     print(f"{v.violation.type}: {v.explanation.title}")
    ...     print(f"  Spec: {v.explanation.spec_reference.name}")
    ...     print(f"  Fix: {v.explanation.fix_suggestions[0]}")
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

# Decision tracking imports
from .decisions import (
    Alternative,
    Decision,
    DecisionStore,
    PlacementRationale,
    RoutingRationale,
    get_decisions_path,
)
from .formatters import (
    FORMATTERS,
    VIOLATION_FORMATTERS,
    format_result,
    format_violations,
)
from .mistakes import (
    Mistake,
    MistakeCategory,
    MistakeCheck,
    MistakeDetector,
    detect_mistakes,
    get_default_checks,
)
from .models import (
    ExplainedViolation,
    ExplanationResult,
    InterfaceSpec,
    RuleExplanation,
    SpecReference,
)
from .rationale import (
    explain_placement,
    explain_route,
    get_decision_store,
    get_decisions,
    record_decision,
    save_decisions,
)
from .registry import ExplanationRegistry

if TYPE_CHECKING:
    pass

__all__ = [
    # Main API functions
    "explain",
    "explain_violations",
    "explain_net_constraints",
    "list_rules",
    "search_rules",
    # Mistake detection
    "Mistake",
    "MistakeCategory",
    "MistakeCheck",
    "MistakeDetector",
    "detect_mistakes",
    "get_default_checks",
    # Models
    "ExplanationResult",
    "ExplainedViolation",
    "RuleExplanation",
    "SpecReference",
    "InterfaceSpec",
    # Registry
    "ExplanationRegistry",
    # Formatters
    "format_result",
    "format_violations",
    "FORMATTERS",
    "VIOLATION_FORMATTERS",
    # Decision tracking
    "Decision",
    "DecisionStore",
    "Alternative",
    "PlacementRationale",
    "RoutingRationale",
    "get_decisions_path",
    "record_decision",
    "get_decisions",
    "explain_placement",
    "explain_route",
    "get_decision_store",
    "save_decisions",
]


def explain(
    rule_id: str,
    context: dict[str, Any] | None = None,
) -> ExplanationResult:
    """Explain a specific design rule constraint.

    This function looks up the explanation for a rule and contextualizes
    it with any provided values (e.g., current vs required measurements).

    Args:
        rule_id: The rule identifier (e.g., "trace_clearance", "via_drill")
        context: Optional context dictionary with values like:
            - net1, net2: Net names involved
            - value, current_value: Current measured value
            - required_value: Required minimum value
            - location: (x, y) tuple of violation location
            - manufacturer: Target manufacturer

    Returns:
        ExplanationResult with rule explanation, spec reference, and fix suggestions

    Raises:
        ValueError: If the rule_id is not found in the registry

    Example:
        >>> result = explain("trace_clearance", {"value": 0.15, "required_value": 0.2})
        >>> print(result.fix_suggestions[0])
        Increase spacing by at least 0.05mm to meet minimum clearance
    """
    context = context or {}

    # Look up the rule explanation
    explanation = ExplanationRegistry.get(rule_id)

    if explanation is None:
        # Try to find a partial match
        matches = ExplanationRegistry.search(rule_id)
        if matches:
            explanation = matches[0]
        else:
            raise ValueError(f"Unknown rule: {rule_id!r}. Use list_rules() to see available rules.")

    # Extract values from context
    current_value = context.get("value") or context.get("current_value")
    required_value = context.get("required_value")
    unit = context.get("unit", "mm")

    # Generate contextualized fix suggestions
    fix_suggestions = _generate_fix_suggestions(
        explanation, context, current_value, required_value, unit
    )

    # Get primary spec reference
    spec_ref = explanation.spec_references[0] if explanation.spec_references else None

    return ExplanationResult(
        rule=explanation.rule_id,
        title=explanation.title,
        explanation=explanation.explanation,
        spec_reference=spec_ref,
        current_value=current_value,
        required_value=required_value,
        unit=unit,
        severity=explanation.severity,
        fix_suggestions=fix_suggestions,
        related_rules=explanation.related_rules,
        context=context,
    )


def explain_violations(
    violations: list[Any],
) -> list[ExplainedViolation]:
    """Explain a list of DRC violations.

    Takes a list of DRCViolation objects and attaches explanations
    to each one based on its type.

    Args:
        violations: List of DRCViolation objects from kicad_tools.drc

    Returns:
        List of ExplainedViolation objects with explanations attached

    Example:
        >>> from kicad_tools.drc import DRCReport
        >>> report = DRCReport.load("design-drc.rpt")
        >>> explained = explain_violations(report.violations)
        >>> for ev in explained:
        ...     print(f"{ev.violation.message}")
        ...     print(f"  Fix: {ev.explanation.fix_suggestions[0]}")
    """
    results = []

    for violation in violations:
        # Get rule_id from the violation
        rule_id = _get_rule_id_from_violation(violation)

        # Build context from violation
        context = _build_context_from_violation(violation)

        # Try to explain, falling back to a generic explanation
        try:
            exp_result = explain(rule_id, context)
        except ValueError:
            exp_result = _create_generic_explanation(violation)

        results.append(ExplainedViolation(violation=violation, explanation=exp_result))

    return results


def explain_net_constraints(
    net_name: str,
    interface_type: str | None = None,
) -> ExplanationResult:
    """Explain why a net has certain constraints.

    This function explains the constraints applied to a net based on
    its interface type (USB, SPI, I2C, etc.) or inferred from its name.

    Args:
        net_name: Name of the net (e.g., "USB_D+", "SCL", "MISO")
        interface_type: Optional explicit interface type override

    Returns:
        ExplanationResult explaining the net's constraints

    Example:
        >>> result = explain_net_constraints("USB_D+")
        >>> print(result.title)
        USB 2.0 High Speed - Differential Impedance
        >>> print(result.explanation)
        USB 2.0 high-speed signaling requires 90Ω differential impedance...
    """
    # Infer interface type from net name if not provided
    if interface_type is None:
        interface_type = _infer_interface_type(net_name)

    if interface_type is None:
        return ExplanationResult(
            rule="unknown_net",
            title=f"Net: {net_name}",
            explanation=f"No specific interface type detected for net '{net_name}'. "
            "Standard design rules apply.",
            severity="info",
        )

    # Get interface spec
    spec = ExplanationRegistry.get_interface(interface_type)

    if spec is None:
        return ExplanationResult(
            rule=f"{interface_type}_unknown",
            title=f"Interface: {interface_type}",
            explanation=f"Interface type '{interface_type}' is not documented in the registry.",
            severity="warning",
        )

    # Build explanation from interface constraints
    constraints_desc = []
    for name, data in spec.constraints.items():
        if "value" in data:
            constraints_desc.append(f"- {name}: {data['value']}")
        elif "max_skew_ps" in data:
            constraints_desc.append(f"- {name}: max {data['max_skew_ps']}ps skew")

    return ExplanationResult(
        rule=f"{interface_type}_constraints",
        title=f"{spec.interface} Constraints",
        explanation=f"Net '{net_name}' is part of a {spec.interface} interface.\n\n"
        f"Derived constraints:\n" + "\n".join(constraints_desc),
        spec_reference=SpecReference(
            name=spec.spec_document,
            url=spec.spec_url,
        ),
        severity="info",
        context={"net_name": net_name, "interface": interface_type},
    )


def list_rules() -> list[str]:
    """List all available rule IDs.

    Returns:
        Sorted list of rule ID strings
    """
    return ExplanationRegistry.list_rules()


def search_rules(query: str) -> list[RuleExplanation]:
    """Search for rules matching a query.

    Args:
        query: Search term to match against rule IDs and titles

    Returns:
        List of matching RuleExplanation objects
    """
    return ExplanationRegistry.search(query)


# =============================================================================
# Internal helper functions
# =============================================================================


def _generate_fix_suggestions(
    explanation: RuleExplanation,
    context: dict[str, Any],
    current_value: float | None,
    required_value: float | None,
    unit: str,
) -> list[str]:
    """Generate contextualized fix suggestions.

    Args:
        explanation: The rule explanation
        context: Context dictionary
        current_value: Current measured value
        required_value: Required minimum value
        unit: Unit of measurement

    Returns:
        List of fix suggestion strings
    """
    suggestions = []

    # Calculate delta if we have both values
    delta = None
    if current_value is not None and required_value is not None:
        delta = required_value - current_value

    # Process fix templates
    for template in explanation.fix_templates:
        suggestion = template
        # Replace placeholders
        if "{delta}" in suggestion and delta is not None:
            suggestion = suggestion.replace("{delta}", f"{delta:.3f}")
        if "{required}" in suggestion and required_value is not None:
            suggestion = suggestion.replace("{required}", f"{required_value}")
        if "{current}" in suggestion and current_value is not None:
            suggestion = suggestion.replace("{current}", f"{current_value}")
        if "{unit}" in suggestion:
            suggestion = suggestion.replace("{unit}", unit)
        if "{net1}" in suggestion:
            suggestion = suggestion.replace("{net1}", context.get("net1", "NET1"))
        if "{net2}" in suggestion:
            suggestion = suggestion.replace("{net2}", context.get("net2", "NET2"))

        suggestions.append(suggestion)

    # Generate default suggestion if none from templates
    if not suggestions and delta is not None:
        rule_id = explanation.rule_id.lower()
        if "clearance" in rule_id:
            suggestions.append(
                f"Increase spacing by at least {delta:.3f}{unit} to meet minimum clearance"
            )
        elif "width" in rule_id or "trace" in rule_id:
            suggestions.append(f"Increase width to at least {required_value}{unit}")
        elif "via" in rule_id:
            suggestions.append(f"Adjust via size to meet {required_value}{unit} minimum")
        else:
            suggestions.append(
                f"Adjust value from {current_value}{unit} to at least {required_value}{unit}"
            )

    return suggestions


def _get_rule_id_from_violation(violation: Any) -> str:
    """Extract a rule ID from a DRC violation.

    Args:
        violation: A DRCViolation object

    Returns:
        Rule ID string
    """
    # Try various attributes that might contain the rule ID
    if hasattr(violation, "rule_id"):
        return violation.rule_id
    if hasattr(violation, "rule"):
        return violation.rule
    if hasattr(violation, "type"):
        vtype = violation.type
        if hasattr(vtype, "value"):
            return vtype.value
        return str(vtype)
    if hasattr(violation, "type_str"):
        return violation.type_str

    return "unknown"


def _build_context_from_violation(violation: Any) -> dict[str, Any]:
    """Build a context dictionary from a DRC violation.

    Args:
        violation: A DRCViolation object

    Returns:
        Context dictionary
    """
    context: dict[str, Any] = {}

    if hasattr(violation, "required_value_mm"):
        context["required_value"] = violation.required_value_mm
    if hasattr(violation, "actual_value_mm"):
        context["value"] = violation.actual_value_mm
    if hasattr(violation, "nets") and violation.nets:
        if len(violation.nets) >= 1:
            context["net1"] = violation.nets[0]
        if len(violation.nets) >= 2:
            context["net2"] = violation.nets[1]
    if hasattr(violation, "primary_location") and violation.primary_location:
        loc = violation.primary_location
        context["location"] = (loc.x_mm, loc.y_mm)

    return context


def _create_generic_explanation(violation: Any) -> ExplanationResult:
    """Create a generic explanation for an unknown violation type.

    Args:
        violation: A DRCViolation object

    Returns:
        ExplanationResult with generic information
    """
    rule_id = _get_rule_id_from_violation(violation)
    message = getattr(violation, "message", str(violation))

    return ExplanationResult(
        rule=rule_id,
        title=rule_id.replace("_", " ").title(),
        explanation=f"This violation indicates a design rule check failure. Message: {message}",
        severity=getattr(violation, "severity", "error"),
        fix_suggestions=["Review the design at the indicated location and adjust accordingly."],
    )


def _infer_interface_type(net_name: str) -> str | None:
    """Infer the interface type from a net name.

    Args:
        net_name: Name of the net

    Returns:
        Interface type string or None if not detected
    """
    net_upper = net_name.upper()

    # USB signals
    if any(x in net_upper for x in ["USB_D+", "USB_D-", "USB_DP", "USB_DM", "VBUS"]):
        return "usb_20_high_speed"

    # I2C signals
    if any(x in net_upper for x in ["SDA", "SCL", "I2C"]):
        return "i2c"

    # SPI signals
    if any(x in net_upper for x in ["MOSI", "MISO", "SCLK", "SCK", "SPI_"]):
        return "spi"

    # UART signals
    if any(x in net_upper for x in ["UART_TX", "UART_RX", "TXD", "RXD"]):
        return "uart"

    # JTAG signals
    if any(x in net_upper for x in ["TDI", "TDO", "TCK", "TMS", "JTAG"]):
        return "jtag"

    return None

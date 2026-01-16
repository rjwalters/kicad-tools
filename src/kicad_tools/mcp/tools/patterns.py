"""MCP tools for pattern validation and adaptation.

Provides tools for validating circuit pattern implementations and
adapting patterns for specific components.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.exceptions import ParseError
from kicad_tools.patterns import (
    PatternAdapter,
    PatternValidator,
    get_component_requirements,
    list_components,
)
from kicad_tools.schema.pcb import PCB


@dataclass
class PatternValidationResultMCP:
    """MCP-friendly pattern validation result."""

    passed: bool
    pattern_type: str
    error_count: int
    warning_count: int
    info_count: int
    rules_checked: int
    violations: list[dict[str, Any]]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for MCP response."""
        return asdict(self)


def validate_pattern(
    pcb_path: str,
    pattern_type: Literal["ldo", "decoupling", "buck"],
    components: dict[str, Any],
) -> dict[str, Any]:
    """Validate a circuit pattern implementation.

    Checks that the pattern meets design requirements including
    placement rules, routing constraints, and component values.

    Args:
        pcb_path: Absolute path to .kicad_pcb file
        pattern_type: Type of pattern to validate ("ldo", "decoupling", "buck")
        components: Pattern-specific component references:
            For LDO:
                - regulator: Reference of the LDO (e.g., "U1")
                - input_cap: Reference of input capacitor (e.g., "C1")
                - output_caps: List of output capacitor references
            For Decoupling:
                - ic: Reference of the IC (e.g., "U1")
                - capacitors: List of decoupling capacitor references
            For Buck:
                - regulator: Reference of the switching regulator
                - inductor: Reference of the inductor
                - input_cap: Reference of input capacitor
                - output_cap: Reference of output capacitor
                - diode: Reference of catch diode (optional)

    Returns:
        Dictionary with validation results including:
        - passed: Whether validation passed (no errors)
        - pattern_type: The pattern type validated
        - error_count, warning_count, info_count: Violation counts
        - violations: List of violation details
        - summary: Human-readable summary

    Raises:
        FileNotFoundError: If PCB file doesn't exist
        ParseError: If PCB file cannot be parsed
        ValueError: If pattern type is unknown

    Example:
        >>> result = validate_pattern(
        ...     "/path/to/board.kicad_pcb",
        ...     "ldo",
        ...     {
        ...         "regulator": "U1",
        ...         "input_cap": "C1",
        ...         "output_caps": ["C2", "C3"]
        ...     }
        ... )
        >>> if not result["passed"]:
        ...     for v in result["violations"]:
        ...         print(f"{v['severity']}: {v['message']}")
    """
    # Validate path
    path = Path(pcb_path)
    if not path.exists():
        raise KiCadFileNotFoundError(f"PCB file not found: {pcb_path}")

    if path.suffix != ".kicad_pcb":
        raise ParseError(f"Invalid file extension: {path.suffix} (expected .kicad_pcb)")

    # Load PCB
    pcb = PCB.load(str(path))

    # Create validator and validate based on pattern type
    validator = PatternValidator()
    pattern_type_lower = pattern_type.lower()

    if pattern_type_lower == "ldo":
        result = validator.validate_ldo_pattern(
            pcb,
            regulator=components.get("regulator", "U1"),
            input_cap=components.get("input_cap", "C1"),
            output_caps=components.get("output_caps", ["C2", "C3"]),
            max_input_cap_distance_mm=components.get("max_input_cap_distance_mm", 3.0),
            max_output_cap_distance_mm=components.get("max_output_cap_distance_mm", 5.0),
            min_input_cap_uf=components.get("min_input_cap_uf", 10.0),
            min_output_cap_uf=components.get("min_output_cap_uf", 10.0),
        )
    elif pattern_type_lower == "decoupling":
        result = validator.validate_decoupling_pattern(
            pcb,
            ic=components.get("ic", "U1"),
            capacitors=components.get("capacitors", ["C1"]),
            max_distance_mm=components.get("max_distance_mm", 5.0),
            required_values=components.get("required_values"),
        )
    elif pattern_type_lower == "buck":
        result = validator.validate_buck_converter_pattern(
            pcb,
            regulator=components.get("regulator", "U1"),
            inductor=components.get("inductor", "L1"),
            input_cap=components.get("input_cap", "C1"),
            output_cap=components.get("output_cap", "C2"),
            diode=components.get("diode"),
            max_inductor_distance_mm=components.get("max_inductor_distance_mm", 5.0),
            max_cap_distance_mm=components.get("max_cap_distance_mm", 10.0),
        )
    else:
        raise ValueError(
            f"Unknown pattern type: {pattern_type}. Supported types: ldo, decoupling, buck"
        )

    # Convert to MCP-friendly format
    return PatternValidationResultMCP(
        passed=result.passed,
        pattern_type=result.pattern_type,
        error_count=result.error_count,
        warning_count=result.warning_count,
        info_count=result.info_count,
        rules_checked=result.rules_checked,
        violations=[v.to_dict() for v in result.violations],
        summary=result.summary(),
    ).to_dict()


def adapt_pattern(
    pattern_type: str,
    component_mpn: str,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Get adapted parameters for a pattern based on component requirements.

    Loads component requirements from the database and generates
    appropriate pattern parameters for the specified component.

    Args:
        pattern_type: Type of pattern ("LDO", "BuckConverter", "Decoupling")
        component_mpn: Manufacturer part number of the main component
        overrides: Optional parameter overrides

    Returns:
        Dictionary with adapted parameters:
        - pattern_type: The pattern type
        - component_mpn: The component MPN
        - parameters: Dictionary of adapted parameters
        - notes: List of notes about the adaptation

    Example:
        >>> result = adapt_pattern("LDO", "AMS1117-3.3")
        >>> print(result["parameters"]["input_cap"])
        '10uF'
    """
    adapter = PatternAdapter()
    params = adapter.adapt(
        pattern_type,
        component_mpn,
        **(overrides or {}),
    )
    return params.to_dict()


def get_requirements(component_mpn: str) -> dict[str, Any]:
    """Get component requirements from the database.

    Retrieves all specifications and requirements for a component,
    useful for understanding design constraints.

    Args:
        component_mpn: Manufacturer part number

    Returns:
        Dictionary with component requirements:
        - mpn: Part number
        - component_type: Type (LDO, BuckConverter, IC, etc.)
        - input_cap: Input capacitor requirements (if applicable)
        - output_cap: Output capacitor requirements (if applicable)
        - dropout_voltage: Dropout voltage (for LDOs)
        - ... and more fields depending on component type

    Raises:
        KeyError: If component not found in database

    Example:
        >>> reqs = get_requirements("AMS1117-3.3")
        >>> print(f"Min input cap: {reqs['input_cap']['min_uf']}uF")
    """
    reqs = get_component_requirements(component_mpn)
    return reqs.to_dict()


def list_available_components(
    component_type: str | None = None,
) -> dict[str, Any]:
    """List components available in the database.

    Args:
        component_type: Optional filter by type (LDO, BuckConverter, IC)

    Returns:
        Dictionary with:
        - components: List of available part numbers
        - count: Total count
        - filter: Applied filter (if any)

    Example:
        >>> result = list_available_components("LDO")
        >>> print(result["components"])
        ['AMS1117-3.3', 'AMS1117-5.0', 'XC6206P332MR', ...]
    """
    components = list_components(component_type)
    return {
        "components": components,
        "count": len(components),
        "filter": component_type,
    }

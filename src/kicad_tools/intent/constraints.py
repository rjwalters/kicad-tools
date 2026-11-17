"""
Constraint derivation from design intent.

This module provides utilities for deriving constraints from intent
declarations and validating them against interface specifications.

Example::

    from kicad_tools.intent.constraints import derive_constraints, validate_intent
    from kicad_tools.intent.registry import REGISTRY

    # Derive constraints from an interface declaration
    constraints = derive_constraints(
        interface_type="usb2_high_speed",
        nets=["USB_DP", "USB_DM"],
        params={"connector": "J1"},
        registry=REGISTRY,
    )

    # Validate an intent declaration
    errors = validate_intent(
        interface_type="usb2_high_speed",
        nets=["USB_DP", "USB_DM"],
        registry=REGISTRY,
    )
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types import Constraint, IntentDeclaration

if TYPE_CHECKING:
    from .registry import InterfaceRegistry


def derive_constraints(
    interface_type: str,
    nets: list[str],
    params: dict[str, object] | None = None,
    registry: InterfaceRegistry | None = None,
) -> list[Constraint]:
    """Derive constraints from an interface declaration.

    Looks up the interface specification and generates the appropriate
    constraints for the given nets.

    Args:
        interface_type: The interface type name (e.g., "usb2_high_speed").
        nets: List of net names in the interface.
        params: Additional parameters for constraint derivation.
        registry: Interface registry to use. Uses global REGISTRY if None.

    Returns:
        List of constraints derived from the interface specification.

    Raises:
        ValueError: If the interface type is not registered.
    """
    if registry is None:
        from .registry import REGISTRY

        registry = REGISTRY

    spec = registry.get(interface_type)
    if spec is None:
        raise ValueError(
            f"Unknown interface type: '{interface_type}'. "
            f"Available types: {', '.join(registry.list_interfaces()) or '(none)'}"
        )

    return spec.derive_constraints(nets, params or {})


def validate_intent(
    interface_type: str,
    nets: list[str],
    registry: InterfaceRegistry | None = None,
) -> list[str]:
    """Validate an intent declaration against its interface specification.

    Checks whether the nets are appropriate for the specified interface type.

    Args:
        interface_type: The interface type name to validate against.
        nets: List of net names to validate.
        registry: Interface registry to use. Uses global REGISTRY if None.

    Returns:
        List of validation error messages. Empty list if valid.

    Raises:
        ValueError: If the interface type is not registered.
    """
    if registry is None:
        from .registry import REGISTRY

        registry = REGISTRY

    spec = registry.get(interface_type)
    if spec is None:
        raise ValueError(
            f"Unknown interface type: '{interface_type}'. "
            f"Available types: {', '.join(registry.list_interfaces()) or '(none)'}"
        )

    return spec.validate_nets(nets)


def create_intent_declaration(
    interface_type: str,
    nets: list[str],
    params: dict[str, object] | None = None,
    metadata: dict[str, object] | None = None,
    registry: InterfaceRegistry | None = None,
    validate: bool = True,
) -> IntentDeclaration:
    """Create an intent declaration with derived constraints.

    Convenience function that validates the interface, derives constraints,
    and creates an IntentDeclaration in one step.

    Args:
        interface_type: The interface type name.
        nets: List of net names in the interface.
        params: Parameters for constraint derivation.
        metadata: Additional metadata for the declaration.
        registry: Interface registry to use. Uses global REGISTRY if None.
        validate: Whether to validate nets before creating declaration.

    Returns:
        An IntentDeclaration with derived constraints.

    Raises:
        ValueError: If the interface type is not registered or validation fails.
    """
    if registry is None:
        from .registry import REGISTRY

        registry = REGISTRY

    if validate:
        errors = validate_intent(interface_type, nets, registry)
        if errors:
            raise ValueError(
                f"Invalid intent declaration for '{interface_type}': " + "; ".join(errors)
            )

    constraints = derive_constraints(interface_type, nets, params, registry)

    return IntentDeclaration(
        interface_type=interface_type,
        nets=nets,
        constraints=constraints,
        metadata=metadata or {},
    )

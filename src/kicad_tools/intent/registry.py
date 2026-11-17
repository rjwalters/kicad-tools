"""
Interface type registry.

This module provides a registry for interface specifications, enabling
lookup and organization of available interface types.

Example::

    from kicad_tools.intent.registry import REGISTRY, InterfaceRegistry
    from kicad_tools.intent.types import InterfaceCategory

    # Check if an interface is registered
    if spec := REGISTRY.get("usb2_high_speed"):
        print(f"Found: {spec.name}")

    # List all interfaces
    for name in REGISTRY.list_interfaces():
        print(f"- {name}")

    # List interfaces by category
    differential_interfaces = REGISTRY.list_by_category(
        InterfaceCategory.DIFFERENTIAL
    )
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .protocol import InterfaceSpec
    from .types import InterfaceCategory


class InterfaceRegistry:
    """Registry of available interface types.

    The registry provides centralized management of interface specifications,
    enabling lookup by name and filtering by category.

    Attributes:
        _interfaces: Internal mapping of interface names to specifications.
    """

    def __init__(self) -> None:
        """Initialize an empty registry."""
        self._interfaces: dict[str, InterfaceSpec] = {}

    def register(self, spec: InterfaceSpec) -> None:
        """Register an interface specification.

        Args:
            spec: The interface specification to register.

        Raises:
            ValueError: If an interface with the same name is already registered.
        """
        if spec.name in self._interfaces:
            raise ValueError(
                f"Interface '{spec.name}' is already registered. "
                "Use a unique name or unregister the existing interface first."
            )
        self._interfaces[spec.name] = spec

    def unregister(self, name: str) -> bool:
        """Unregister an interface specification.

        Args:
            name: The name of the interface to unregister.

        Returns:
            True if the interface was unregistered, False if it wasn't registered.
        """
        if name in self._interfaces:
            del self._interfaces[name]
            return True
        return False

    def get(self, name: str) -> InterfaceSpec | None:
        """Get interface spec by name.

        Args:
            name: The interface type name to look up.

        Returns:
            The interface specification, or None if not found.
        """
        return self._interfaces.get(name)

    def list_interfaces(self) -> list[str]:
        """List all registered interface names.

        Returns:
            Sorted list of all registered interface type names.
        """
        return sorted(self._interfaces.keys())

    def list_by_category(self, category: InterfaceCategory) -> list[str]:
        """List interfaces in a category.

        Args:
            category: The category to filter by.

        Returns:
            Sorted list of interface names in the specified category.
        """
        return sorted(name for name, spec in self._interfaces.items() if spec.category == category)

    def __len__(self) -> int:
        """Return the number of registered interfaces."""
        return len(self._interfaces)

    def __contains__(self, name: str) -> bool:
        """Check if an interface is registered."""
        return name in self._interfaces


# Global registry with built-in interfaces
REGISTRY = InterfaceRegistry()

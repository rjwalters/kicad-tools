"""
Pattern registry for managing PCB patterns.

This module provides a global registry for PCB patterns, allowing patterns
to be registered by name and retrieved for use in design workflows.

Example::

    from kicad_tools.patterns import PatternRegistry, LDOPattern

    # Register a pattern class
    PatternRegistry.register("ldo", LDOPattern)

    # Register a pattern instance
    pattern = LDOPattern(regulator="AMS1117-3.3")
    PatternRegistry.register_instance("my_ldo", pattern)

    # Get a registered pattern
    pattern = PatternRegistry.get("ldo")

    # Load patterns from YAML
    PatternRegistry.load_yaml("patterns/my_sensor.yaml")

    # List all registered patterns
    for name in PatternRegistry.list():
        print(f"  - {name}")
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .base import PCBPattern


class PatternRegistry:
    """Global registry for PCB patterns.

    The registry supports two types of registrations:
    1. Pattern classes - Templates that can be instantiated with config
    2. Pattern instances - Pre-configured patterns ready to use

    Patterns can be registered programmatically or loaded from YAML files.
    """

    _patterns: dict[str, type[PCBPattern]] = {}
    _instances: dict[str, PCBPattern] = {}
    _metadata: dict[str, dict[str, Any]] = {}

    @classmethod
    def register(
        cls,
        name: str,
        pattern_class: type[PCBPattern],
        description: str = "",
        category: str = "",
    ) -> None:
        """Register a pattern class.

        Args:
            name: Unique name for the pattern
            pattern_class: The PCBPattern subclass to register
            description: Human-readable description
            category: Category for organizing patterns (e.g., "power", "interface")

        Raises:
            ValueError: If name is already registered
        """
        if name in cls._patterns or name in cls._instances:
            raise ValueError(f"Pattern '{name}' is already registered")

        cls._patterns[name] = pattern_class
        cls._metadata[name] = {
            "description": description,
            "category": category,
            "type": "class",
        }

    @classmethod
    def register_instance(
        cls,
        name: str,
        pattern: PCBPattern,
        description: str = "",
        category: str = "",
    ) -> None:
        """Register a pre-configured pattern instance.

        Use this for patterns loaded from YAML or pre-configured patterns
        that don't need additional configuration.

        Args:
            name: Unique name for the pattern
            pattern: The configured PCBPattern instance
            description: Human-readable description
            category: Category for organizing patterns

        Raises:
            ValueError: If name is already registered
        """
        if name in cls._patterns or name in cls._instances:
            raise ValueError(f"Pattern '{name}' is already registered")

        cls._instances[name] = pattern
        cls._metadata[name] = {
            "description": description,
            "category": category,
            "type": "instance",
        }

    @classmethod
    def get(cls, name: str, **kwargs: Any) -> PCBPattern:
        """Get a pattern by name.

        For registered classes, kwargs are passed to the constructor.
        For registered instances, kwargs are ignored.

        Args:
            name: Name of the registered pattern
            **kwargs: Configuration parameters for class-based patterns

        Returns:
            A PCBPattern instance

        Raises:
            KeyError: If no pattern with that name exists
        """
        if name in cls._instances:
            return cls._instances[name]

        if name in cls._patterns:
            return cls._patterns[name](**kwargs)

        available = ", ".join(sorted(cls.list()))
        raise KeyError(f"Unknown pattern '{name}'. Available: {available}")

    @classmethod
    def get_class(cls, name: str) -> type[PCBPattern]:
        """Get a pattern class by name.

        This returns the class itself, not an instance, for patterns
        registered as classes.

        Args:
            name: Name of the registered pattern

        Returns:
            The PCBPattern subclass

        Raises:
            KeyError: If no pattern class with that name exists
        """
        if name not in cls._patterns:
            raise KeyError(f"No pattern class registered as '{name}'")
        return cls._patterns[name]

    @classmethod
    def has(cls, name: str) -> bool:
        """Check if a pattern is registered.

        Args:
            name: Name to check

        Returns:
            True if pattern exists, False otherwise
        """
        return name in cls._patterns or name in cls._instances

    @classmethod
    def list(cls, category: str | None = None) -> list[str]:
        """List all registered pattern names.

        Args:
            category: If provided, only list patterns in this category

        Returns:
            List of pattern names, sorted alphabetically
        """
        all_names = set(cls._patterns.keys()) | set(cls._instances.keys())

        if category is not None:
            all_names = {
                name
                for name in all_names
                if cls._metadata.get(name, {}).get("category") == category
            }

        return sorted(all_names)

    @classmethod
    def get_metadata(cls, name: str) -> dict[str, Any]:
        """Get metadata for a registered pattern.

        Args:
            name: Name of the pattern

        Returns:
            Dict with description, category, and type

        Raises:
            KeyError: If pattern not found
        """
        if name not in cls._metadata:
            raise KeyError(f"No pattern registered as '{name}'")
        return cls._metadata[name].copy()

    @classmethod
    def categories(cls) -> list[str]:
        """Get all pattern categories.

        Returns:
            Sorted list of unique categories
        """
        cats = {meta.get("category", "") for meta in cls._metadata.values()}
        cats.discard("")
        return sorted(cats)

    @classmethod
    def load_yaml(cls, path: str | Path) -> str:
        """Load a pattern from a YAML file.

        The pattern is registered using the name from the YAML file.

        Args:
            path: Path to the YAML pattern file

        Returns:
            The name the pattern was registered under

        Raises:
            FileNotFoundError: If the file doesn't exist
            ValueError: If the YAML is invalid
        """
        # Import here to avoid circular imports
        from .loader import PatternLoader

        loader = PatternLoader()
        pattern, metadata = loader.load(path)

        name = metadata.get("name", Path(path).stem)
        description = metadata.get("description", "")
        category = metadata.get("category", "custom")

        cls.register_instance(name, pattern, description=description, category=category)
        return name

    @classmethod
    def load_directory(cls, directory: str | Path) -> list[str]:
        """Load all YAML patterns from a directory.

        Args:
            directory: Path to directory containing YAML files

        Returns:
            List of pattern names that were loaded
        """
        directory = Path(directory)
        if not directory.is_dir():
            raise ValueError(f"'{directory}' is not a directory")

        loaded = []
        for yaml_file in sorted(directory.glob("*.yaml")):
            try:
                name = cls.load_yaml(yaml_file)
                loaded.append(name)
            except (ValueError, KeyError) as e:
                # Log warning but continue loading other files
                import warnings

                warnings.warn(f"Failed to load {yaml_file}: {e}", stacklevel=2)

        return loaded

    @classmethod
    def unregister(cls, name: str) -> None:
        """Remove a pattern from the registry.

        Args:
            name: Name of the pattern to remove

        Raises:
            KeyError: If pattern not found
        """
        if name in cls._patterns:
            del cls._patterns[name]
        elif name in cls._instances:
            del cls._instances[name]
        else:
            raise KeyError(f"No pattern registered as '{name}'")

        if name in cls._metadata:
            del cls._metadata[name]

    @classmethod
    def clear(cls) -> None:
        """Remove all registered patterns.

        Useful for testing or resetting state.
        """
        cls._patterns.clear()
        cls._instances.clear()
        cls._metadata.clear()


def register_pattern(
    name: str,
    description: str = "",
    category: str = "",
) -> Any:
    """Decorator to register a pattern class.

    Example::

        @register_pattern("my_sensor", description="NTC sensor interface")
        class MySensorPattern(PCBPattern):
            ...

    Args:
        name: Name to register under
        description: Human-readable description
        category: Category for organization

    Returns:
        Decorator function
    """

    def decorator(cls: type[PCBPattern]) -> type[PCBPattern]:
        PatternRegistry.register(name, cls, description=description, category=category)
        return cls

    return decorator

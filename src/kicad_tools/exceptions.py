"""
Custom exception hierarchy for kicad-tools.

Provides consistent error handling with context, suggestions, and actionable guidance.
All exceptions include:
- Context information (file paths, line numbers, etc.)
- Suggestions for how to fix the issue
- Clear, formatted error messages

Example::

    from kicad_tools.exceptions import FileFormatError, ValidationError

    # Raise with context and suggestions
    raise FileFormatError(
        "Invalid KiCad schematic format",
        context={"file": "project.kicad_sch", "expected": "kicad_sch", "got": "kicad_pcb"},
        suggestions=["Check that the file is a schematic, not a PCB file"]
    )

    # Validation with multiple errors
    errors = ["Field 'name' is required", "Invalid email format"]
    raise ValidationError(errors, context={"file": "config.json"})
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Union


class KiCadToolsError(Exception):
    """
    Base exception for all kicad-tools errors.

    Provides consistent formatting with context and suggestions.

    Attributes:
        context: Dictionary of contextual information (file, line, etc.)
        suggestions: List of actionable suggestions for fixing the error
    """

    def __init__(
        self,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        suggestions: Optional[List[str]] = None,
    ):
        self.message = message
        self.context = context or {}
        self.suggestions = suggestions or []
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        """Format the error message with context and suggestions."""
        parts = [self.message]

        if self.context:
            parts.append("\n\nContext:")
            for key, value in self.context.items():
                parts.append(f"\n  {key}: {value}")

        if self.suggestions:
            parts.append("\n\nSuggestions:")
            for suggestion in self.suggestions:
                parts.append(f"\n  - {suggestion}")

        return "".join(parts)

    def __str__(self) -> str:
        return self._format_message()


class ParseError(KiCadToolsError):
    """
    S-expression or file parsing failed.

    Raised when a KiCad file cannot be parsed due to syntax errors
    or unexpected content.

    Example::

        raise ParseError(
            "Unexpected token in expression",
            context={"file": "project.kicad_sch", "line": 42, "column": 15},
            suggestions=["Check for missing parentheses", "Verify file encoding is UTF-8"]
        )
    """

    def __init__(
        self,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        suggestions: Optional[List[str]] = None,
        line: Optional[int] = None,
        column: Optional[int] = None,
        file_path: Optional[Union[str, Path]] = None,
    ):
        # Build context from convenience parameters
        ctx = context or {}
        if file_path and "file" not in ctx:
            ctx["file"] = str(file_path)
        if line is not None and "line" not in ctx:
            ctx["line"] = line
        if column is not None and "column" not in ctx:
            ctx["column"] = column

        super().__init__(message, ctx, suggestions)


class ValidationError(KiCadToolsError):
    """
    Data validation failed with one or more errors.

    Collects all validation errors instead of failing on the first one,
    providing a complete list of issues to fix.

    Example::

        errors = [
            "Field 'reference' is required",
            "Invalid footprint format: expected 'Library:Footprint'",
            "Duplicate symbol reference: U1"
        ]
        raise ValidationError(errors, context={"file": "project.kicad_sch"})

    Attributes:
        errors: List of individual validation error messages
    """

    def __init__(
        self,
        errors: List[str],
        context: Optional[Dict[str, Any]] = None,
        suggestions: Optional[List[str]] = None,
    ):
        self.errors = errors
        message = f"Validation failed with {len(errors)} error(s):\n"
        message += "\n".join(f"  {i + 1}. {e}" for i, e in enumerate(errors))
        super().__init__(message, context, suggestions)


class FileFormatError(KiCadToolsError):
    """
    File format not recognized or corrupted.

    Raised when a file exists but is not a valid KiCad file
    or is the wrong type (e.g., PCB file when schematic expected).

    Example::

        raise FileFormatError(
            "Not a KiCad schematic file",
            context={"file": "board.kicad_pcb", "expected": "kicad_sch", "got": "kicad_pcb"},
            suggestions=["Use a .kicad_sch file for schematic operations"]
        )
    """

    pass


class FileNotFoundError(KiCadToolsError):
    """
    Required file was not found.

    Extends the base FileNotFoundError to include searched paths
    and suggestions for resolution.

    Example::

        raise FileNotFoundError(
            "Symbol library not found",
            context={
                "library": "Device",
                "searched": ["/usr/share/kicad/symbols", "~/kicad/symbols"]
            },
            suggestions=[
                "Install the KiCad symbol libraries",
                "Set KICAD_SYMBOL_DIR environment variable"
            ]
        )
    """

    pass


class RoutingError(KiCadToolsError):
    """
    PCB routing operation failed.

    Raised when autorouting or trace operations cannot be completed.

    Example::

        raise RoutingError(
            "Cannot route net: GND",
            context={"net": "GND", "from": "U1.GND", "to": "U2.GND", "blocked_by": "trace on F.Cu"},
            suggestions=["Increase clearance settings", "Try a different routing strategy"]
        )
    """

    pass


class ComponentError(KiCadToolsError):
    """
    Component or symbol-related error.

    Raised for issues with symbol references, library lookups,
    or component operations.

    Example::

        raise ComponentError(
            "Symbol not found in library",
            context={"symbol": "LM7805", "library": "Regulator_Linear"},
            suggestions=[
                "Check the library name spelling",
                "Verify the library is installed"
            ]
        )
    """

    pass


class ConfigurationError(KiCadToolsError):
    """
    Configuration or settings error.

    Raised when configuration is invalid, missing, or incompatible.

    Example::

        raise ConfigurationError(
            "Invalid manufacturer configuration",
            context={"manufacturer": "unknown_fab", "available": ["jlcpcb", "pcbway", "oshpark"]},
            suggestions=["Use one of the available manufacturer presets"]
        )
    """

    pass


class ExportError(KiCadToolsError):
    """
    Export operation failed.

    Raised when generating output files (Gerbers, BOM, etc.) fails.

    Example::

        raise ExportError(
            "Gerber export failed",
            context={"output_dir": "/tmp/gerbers", "reason": "KiCad CLI not found"},
            suggestions=[
                "Ensure KiCad is installed",
                "Add KiCad to your PATH"
            ]
        )
    """

    pass


# Re-export built-in exceptions that we want to wrap
__all__ = [
    "KiCadToolsError",
    "ParseError",
    "ValidationError",
    "FileFormatError",
    "FileNotFoundError",
    "RoutingError",
    "ComponentError",
    "ConfigurationError",
    "ExportError",
]

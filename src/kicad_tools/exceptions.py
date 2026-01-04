"""
Custom exception hierarchy for kicad-tools.

Provides consistent error handling with context, suggestions, and actionable guidance.
All exceptions include:
- Context information (file paths, line numbers, etc.)
- Suggestions for how to fix the issue
- Error codes for programmatic handling
- JSON serialization for CLI output

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

    # JSON serialization for CLI
    try:
        load_schematic("missing.kicad_sch")
    except KiCadToolsError as e:
        print(json.dumps(e.to_dict()))
        # {"error_code": "FILE_NOT_FOUND", "message": "...", ...}
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SourcePosition:
    """Position within a KiCad file for precise error reporting.

    Enables errors to point to specific locations in KiCad schematic or PCB files,
    allowing agents and tools to navigate directly to the source of issues.

    Attributes:
        file_path: Path to the KiCad file containing the element
        line: Line number (1-indexed) where the element starts
        column: Column number (1-indexed) within the line
        element_type: Type of element (e.g., "footprint", "track", "via", "symbol")
        element_ref: Reference designator or identifier (e.g., "C1", "R2", "net-VCC")
        position_mm: Optional board/schematic coordinates as (x, y) tuple
        layer: Optional layer name for PCB elements (e.g., "F.Cu", "B.SilkS")

    Example::

        pos = SourcePosition(
            file_path=Path("project.kicad_pcb"),
            line=42,
            column=5,
            element_type="track",
            element_ref="net-VCC",
            position_mm=(25.4, 50.8),
            layer="F.Cu",
        )
        print(pos)  # project.kicad_pcb:42:5
    """

    file_path: Path
    line: int
    column: int
    element_type: str = ""
    element_ref: str = ""
    position_mm: tuple[float, float] | None = None
    layer: str | None = None

    def __str__(self) -> str:
        """Format as 'file:line:column' for IDE/editor integration."""
        return f"{self.file_path}:{self.line}:{self.column}"

    def __repr__(self) -> str:
        parts = [f"file_path={self.file_path!r}", f"line={self.line}", f"column={self.column}"]
        if self.element_type:
            parts.append(f"element_type={self.element_type!r}")
        if self.element_ref:
            parts.append(f"element_ref={self.element_ref!r}")
        if self.position_mm:
            parts.append(f"position_mm={self.position_mm!r}")
        if self.layer:
            parts.append(f"layer={self.layer!r}")
        return f"SourcePosition({', '.join(parts)})"

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dictionary."""
        result: dict[str, Any] = {
            "file_path": str(self.file_path),
            "line": self.line,
            "column": self.column,
        }
        if self.element_type:
            result["element_type"] = self.element_type
        if self.element_ref:
            result["element_ref"] = self.element_ref
        if self.position_mm:
            result["position_mm"] = {"x": self.position_mm[0], "y": self.position_mm[1]}
        if self.layer:
            result["layer"] = self.layer
        return result


class KiCadDiagnostic(Exception):
    """Base exception with source position tracking for precise error reporting.

    Provides compiler-style error messages with file:line:column format,
    enabling IDE integration and direct navigation to error locations.

    Inherits the rich context and suggestions pattern from KiCadToolsError
    while adding source position support for KiCad file elements.

    Attributes:
        message: Human-readable error message
        source: Optional source position in the KiCad file
        sources: List of additional related source positions
        suggestions: List of actionable suggestions for fixing the error

    Example::

        raise KiCadDiagnostic(
            "Track clearance violation",
            source=SourcePosition(
                file_path=Path("board.kicad_pcb"),
                line=142,
                column=3,
                element_type="track",
                element_ref="net-GND",
                position_mm=(25.4, 50.8),
                layer="F.Cu",
            ),
            suggestions=["Increase track spacing to 0.2mm"],
        )
        # Output: board.kicad_pcb:142:3: Track clearance violation
    """

    def __init__(
        self,
        message: str,
        source: SourcePosition | None = None,
        sources: list[SourcePosition] | None = None,
        suggestions: list[str] | None = None,
    ):
        self.message = message
        self.source = source
        self.sources = sources or []
        self.suggestions = suggestions or []
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        """Format message with source position prefix."""
        parts = []

        # Primary location prefix
        if self.source:
            parts.append(f"{self.source}: {self.message}")
        else:
            parts.append(self.message)

        # Additional related locations
        if self.sources:
            parts.append("\n\nRelated locations:")
            for src in self.sources:
                detail = f"  {src}"
                if src.element_ref:
                    detail += f" ({src.element_ref})"
                parts.append(f"\n{detail}")

        # Suggestions
        if self.suggestions:
            parts.append("\n\nSuggestions:")
            for suggestion in self.suggestions:
                parts.append(f"\n  - {suggestion}")

        return "".join(parts)

    def __str__(self) -> str:
        return self._format_message()

    @property
    def location(self) -> str:
        """Get the primary source location as file:line:col string."""
        if self.source:
            return str(self.source)
        return ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dictionary.

        Returns:
            Dictionary with message, source positions, and suggestions.
        """
        result: dict[str, Any] = {
            "message": self.message,
            "suggestions": self.suggestions,
        }
        if self.source:
            result["source"] = self.source.to_dict()
        if self.sources:
            result["related_sources"] = [s.to_dict() for s in self.sources]
        return result


def _class_name_to_error_code(class_name: str) -> str:
    """Convert CamelCase class name to SCREAMING_SNAKE_CASE error code.

    Example: FileNotFoundError -> FILE_NOT_FOUND
    """
    # Remove 'Error' suffix if present
    name = class_name
    if name.endswith("Error"):
        name = name[:-5]

    # Convert CamelCase to SCREAMING_SNAKE_CASE
    # Insert underscore before uppercase letters (except at start)
    result = re.sub(r"(?<!^)(?=[A-Z])", "_", name)
    return result.upper()


class KiCadToolsError(Exception):
    """
    Base exception for all kicad-tools errors.

    Provides consistent formatting with context, suggestions, and error codes.

    Attributes:
        message: Human-readable error message
        error_code: Machine-readable error code for programmatic handling
        context: Dictionary of contextual information (file, line, etc.)
        suggestions: List of actionable suggestions for fixing the error

    Example::

        try:
            do_something()
        except KiCadToolsError as e:
            if e.error_code == "FILE_NOT_FOUND":
                # Handle specifically
                pass
            print(e.to_dict())  # JSON-serializable format
    """

    # Default error code (subclasses can override)
    _default_error_code: str | None = None

    def __init__(
        self,
        message: str,
        context: dict[str, Any] | None = None,
        suggestions: list[str] | None = None,
        error_code: str | None = None,
    ):
        self.message = message
        self.context = context or {}
        self.suggestions = suggestions or []

        # Use provided error_code, or class default, or auto-generate from class name
        if error_code is not None:
            self.error_code = error_code
        elif self._default_error_code is not None:
            self.error_code = self._default_error_code
        else:
            self.error_code = _class_name_to_error_code(self.__class__.__name__)

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

    def to_dict(self) -> dict[str, Any]:
        """Convert exception to a JSON-serializable dictionary.

        Useful for CLI --json output and programmatic error handling.

        Returns:
            Dictionary with error_code, message, context, and suggestions.

        Example::

            >>> err = FileNotFoundError("File not found", context={"file": "x.txt"})
            >>> err.to_dict()
            {
                "error_code": "FILE_NOT_FOUND",
                "message": "File not found",
                "context": {"file": "x.txt"},
                "suggestions": []
            }
        """
        return {
            "error_code": self.error_code,
            "message": self.message,
            "context": self.context,
            "suggestions": self.suggestions,
        }


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
        context: dict[str, Any] | None = None,
        suggestions: list[str] | None = None,
        line: int | None = None,
        column: int | None = None,
        file_path: str | Path | None = None,
        error_code: str | None = None,
    ):
        # Build context from convenience parameters
        ctx = context or {}
        if file_path and "file" not in ctx:
            ctx["file"] = str(file_path)
        if line is not None and "line" not in ctx:
            ctx["line"] = line
        if column is not None and "column" not in ctx:
            ctx["column"] = column

        super().__init__(message, ctx, suggestions, error_code)


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
        errors: list[str],
        context: dict[str, Any] | None = None,
        suggestions: list[str] | None = None,
        error_code: str | None = None,
    ):
        self.errors = errors
        message = f"Validation failed with {len(errors)} error(s):\n"
        message += "\n".join(f"  {i + 1}. {e}" for i, e in enumerate(errors))
        super().__init__(message, context, suggestions, error_code)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary, including individual validation errors."""
        result = super().to_dict()
        result["errors"] = self.errors
        return result


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
    # Source position tracking
    "SourcePosition",
    "KiCadDiagnostic",
    # Base exception
    "KiCadToolsError",
    # Specific exceptions
    "ParseError",
    "ValidationError",
    "FileFormatError",
    "FileNotFoundError",
    "RoutingError",
    "ComponentError",
    "ConfigurationError",
    "ExportError",
]

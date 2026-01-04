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
from pathlib import Path
from typing import Any


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

    def get_snippet(self, context_lines: int = 3) -> str | None:
        """
        Extract source code snippet around the error location.

        Uses the file and line information from context to extract
        surrounding lines for debugging context.

        Args:
            context_lines: Number of lines to show before/after error.

        Returns:
            Formatted snippet string, or None if file/line not available.

        Example::

            try:
                parse_file("broken.kicad_sch")
            except ParseError as e:
                if snippet := e.get_snippet():
                    print(snippet)
        """
        file_str = self.context.get("file")
        line = self.context.get("line")

        if file_str is None or line is None:
            return None

        file_path = Path(file_str)
        if not file_path.exists():
            return None

        try:
            extractor = SExpSnippetExtractor(context_lines=context_lines)
            return extractor.extract(file_path, line)
        except Exception:
            return None

    def format_with_snippet(self, context_lines: int = 3) -> str:
        """
        Format error message with source code snippet included.

        Provides a complete error display with the error message,
        file location, and surrounding code context.

        Args:
            context_lines: Number of lines to show before/after error.

        Returns:
            Formatted error string with snippet (if available).

        Example::

            try:
                parse_file("broken.kicad_sch")
            except ParseError as e:
                print(e.format_with_snippet())
        """
        parts = [self.message]

        file_str = self.context.get("file")
        line = self.context.get("line")

        if file_str and line:
            parts.append("")
            parts.append(f"  {file_str}:{line}")

            snippet = self.get_snippet(context_lines)
            if snippet:
                parts.append("")
                parts.append(snippet)

        if self.suggestions:
            parts.append("")
            parts.append("Suggestions:")
            for suggestion in self.suggestions:
                parts.append(f"  - {suggestion}")

        return "\n".join(parts)


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


class SExpSnippetExtractor:
    """
    Extract S-expression snippets for error context.

    Provides methods to extract and display relevant S-expression code around
    error locations, similar to how compilers show source code around syntax errors.

    Example::

        extractor = SExpSnippetExtractor()

        # Extract lines around an error
        snippet = extractor.extract(Path("board.kicad_pcb"), line=1247)
        print(snippet)
        #    1244 | (fp_line (start 0 0) (end 2.5 0) (layer "F.SilkS"))
        #    1245 | )
        #    1246 | (footprint "Capacitor_SMD:C_0402"
        # -> 1247 |   (at 45.2 32.1)
        #    1248 |   (property "Reference" "C1")
        #    1249 |   (pad "1" smd rect (at -0.5 0) (size 0.5 0.5))
        #    1250 | )

        # Extract element by reference (e.g., footprint)
        element = extractor.extract_element(
            Path("board.kicad_pcb"),
            element_ref="C1",
        )
    """

    def __init__(
        self,
        context_lines: int = 3,
        marker: str = "->",
        line_number_width: int = 4,
    ):
        """
        Initialize the snippet extractor.

        Args:
            context_lines: Default number of lines to show before/after error.
            marker: String to use for marking the error line.
            line_number_width: Minimum width for line number display.
        """
        self.context_lines = context_lines
        self.marker = marker
        self.line_number_width = line_number_width

    def extract(
        self,
        file_path: Path,
        line: int,
        context_lines: int | None = None,
    ) -> str:
        """
        Extract lines around error location with line numbers.

        Args:
            file_path: Path to the KiCad file.
            line: The line number of the error (1-indexed).
            context_lines: Number of lines to show before/after (overrides default).

        Returns:
            Formatted snippet string with line numbers and error marker.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If line number is out of range.

        Example::

            snippet = extractor.extract(Path("board.kicad_pcb"), line=1247)
            # Returns formatted snippet with arrow on line 1247
        """
        ctx = context_lines if context_lines is not None else self.context_lines

        if not file_path.exists():
            raise FileNotFoundError(
                f"File not found: {file_path}",
                context={"file": str(file_path)},
            )

        content = file_path.read_text(encoding="utf-8")
        lines = content.splitlines()

        if line < 1 or line > len(lines):
            raise ValueError(f"Line {line} is out of range (file has {len(lines)} lines)")

        # Calculate range (0-indexed internally)
        start = max(0, line - ctx - 1)
        end = min(len(lines), line + ctx)

        # Determine line number width based on largest line number
        max_line_num = end
        num_width = max(self.line_number_width, len(str(max_line_num)))

        snippet_lines = []
        for i in range(start, end):
            line_num = i + 1  # Convert back to 1-indexed
            content_line = lines[i]

            if line_num == line:
                # Error line - add marker
                prefix = f"{self.marker} {line_num:>{num_width}}"
            else:
                # Context line - spaces for alignment
                prefix = f"   {line_num:>{num_width}}"

            snippet_lines.append(f"{prefix} | {content_line}")

        return "\n".join(snippet_lines)

    def extract_element(
        self,
        file_path: Path,
        element_ref: str,
        element_type: str = "footprint",
    ) -> str | None:
        """
        Extract complete S-expression for an element by reference.

        Finds an element (like a footprint) by its reference designator and
        returns its complete S-expression representation.

        Args:
            file_path: Path to the KiCad file.
            element_ref: Reference designator (e.g., "C1", "U1", "R1").
            element_type: Type of element to search for (default: "footprint").

        Returns:
            The complete S-expression string for the element, or None if not found.

        Example::

            sexp = extractor.extract_element(
                Path("board.kicad_pcb"),
                element_ref="C1",
            )
            # Returns: "(footprint \"Capacitor_SMD:C_0402\"\\n  (at 45.2 32.1)\\n  ...)"
        """
        # Import here to avoid circular imports
        from kicad_tools.sexp import parse_file

        if not file_path.exists():
            raise FileNotFoundError(
                f"File not found: {file_path}",
                context={"file": str(file_path)},
            )

        try:
            doc = parse_file(file_path)
        except Exception as e:
            raise ParseError(
                f"Failed to parse file: {e}",
                context={"file": str(file_path)},
            )

        # Find all elements of the specified type
        elements = doc.find_all(element_type)

        for element in elements:
            # Look for property child with Reference value
            prop = element.get("property")
            if prop is not None:
                # Check if this is a Reference property with matching value
                atoms = prop.get_atoms()
                if len(atoms) >= 2 and atoms[0] == "Reference" and atoms[1] == element_ref:
                    return element.to_string()

            # Also check for properties that might be nested differently
            for child in element.children:
                if child.name == "property":
                    child_atoms = child.get_atoms()
                    if (
                        len(child_atoms) >= 2
                        and child_atoms[0] == "Reference"
                        and child_atoms[1] == element_ref
                    ):
                        return element.to_string()

        return None

    def extract_with_header(
        self,
        file_path: Path,
        line: int,
        message: str | None = None,
        context_lines: int | None = None,
    ) -> str:
        """
        Extract snippet with file location header.

        Combines file path and line information with the snippet for
        complete error context display.

        Args:
            file_path: Path to the KiCad file.
            line: The line number of the error (1-indexed).
            message: Optional message to display before the snippet.
            context_lines: Number of lines to show before/after.

        Returns:
            Formatted string with header and snippet.

        Example::

            output = extractor.extract_with_header(
                Path("board.kicad_pcb"),
                line=1247,
                message="DRC Error: Clearance violation between C1 and U1",
            )
            # Returns:
            # DRC Error: Clearance violation between C1 and U1
            #
            #   board.kicad_pcb:1247
            #
            #    1244 | (fp_line ...)
            #    ...
        """
        parts = []

        if message:
            parts.append(message)
            parts.append("")

        # Add file location
        parts.append(f"  {file_path}:{line}")
        parts.append("")

        # Add snippet
        snippet = self.extract(file_path, line, context_lines)
        parts.append(snippet)

        return "\n".join(parts)


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
    "SExpSnippetExtractor",
]

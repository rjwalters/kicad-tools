"""Tests for kicad_tools.exceptions module."""

import pytest

from kicad_tools.exceptions import (
    ComponentError,
    ConfigurationError,
    ExportError,
    FileFormatError,
    FileNotFoundError,
    KiCadToolsError,
    ParseError,
    RoutingError,
    ValidationError,
)


class TestKiCadToolsError:
    """Tests for the base exception class."""

    def test_basic_message(self):
        """Test basic error message."""
        err = KiCadToolsError("Something went wrong")
        assert str(err) == "Something went wrong"
        assert err.message == "Something went wrong"
        assert err.context == {}
        assert err.suggestions == []

    def test_with_context(self):
        """Test error with context dictionary."""
        err = KiCadToolsError(
            "File operation failed",
            context={"file": "test.kicad_sch", "line": 42},
        )
        msg = str(err)
        assert "File operation failed" in msg
        assert "Context:" in msg
        assert "file: test.kicad_sch" in msg
        assert "line: 42" in msg

    def test_with_suggestions(self):
        """Test error with suggestions list."""
        err = KiCadToolsError(
            "Invalid format",
            suggestions=["Check file encoding", "Verify file is not corrupted"],
        )
        msg = str(err)
        assert "Invalid format" in msg
        assert "Suggestions:" in msg
        assert "Check file encoding" in msg
        assert "Verify file is not corrupted" in msg

    def test_with_context_and_suggestions(self):
        """Test error with both context and suggestions."""
        err = KiCadToolsError(
            "Operation failed",
            context={"operation": "export", "target": "gerbers"},
            suggestions=["Install KiCad", "Check PATH"],
        )
        msg = str(err)
        assert "Operation failed" in msg
        assert "Context:" in msg
        assert "operation: export" in msg
        assert "Suggestions:" in msg
        assert "Install KiCad" in msg


class TestParseError:
    """Tests for ParseError exception."""

    def test_basic_parse_error(self):
        """Test basic parse error."""
        err = ParseError("Unexpected token")
        assert "Unexpected token" in str(err)
        assert isinstance(err, KiCadToolsError)

    def test_with_line_and_column(self):
        """Test parse error with line/column info."""
        err = ParseError(
            "Syntax error",
            line=10,
            column=25,
            file_path="/path/to/file.kicad_sch",
        )
        msg = str(err)
        assert "Syntax error" in msg
        assert "line: 10" in msg
        assert "column: 25" in msg
        assert "file: /path/to/file.kicad_sch" in msg

    def test_context_override(self):
        """Test that explicit context takes precedence."""
        err = ParseError(
            "Error",
            context={"file": "explicit.txt"},
            file_path="convenience.txt",
        )
        # Explicit context should win
        assert "explicit.txt" in str(err)


class TestValidationError:
    """Tests for ValidationError exception."""

    def test_single_error(self):
        """Test validation with single error."""
        err = ValidationError(["Field 'name' is required"])
        msg = str(err)
        assert "Validation failed with 1 error(s)" in msg
        assert "Field 'name' is required" in msg
        assert err.errors == ["Field 'name' is required"]

    def test_multiple_errors(self):
        """Test validation with multiple errors."""
        errors = [
            "Missing required field: reference",
            "Invalid value format",
            "Duplicate symbol ID",
        ]
        err = ValidationError(errors)
        msg = str(err)
        assert "Validation failed with 3 error(s)" in msg
        for error in errors:
            assert error in msg
        assert err.errors == errors

    def test_with_context(self):
        """Test validation error with context."""
        err = ValidationError(
            ["Invalid format"],
            context={"file": "config.json", "section": "settings"},
        )
        msg = str(err)
        assert "file: config.json" in msg
        assert "section: settings" in msg


class TestFileFormatError:
    """Tests for FileFormatError exception."""

    def test_file_format_error(self):
        """Test file format error."""
        err = FileFormatError(
            "Not a KiCad schematic",
            context={"file": "board.kicad_pcb", "expected": "kicad_sch", "got": "kicad_pcb"},
            suggestions=["Use a .kicad_sch file"],
        )
        msg = str(err)
        assert "Not a KiCad schematic" in msg
        assert "expected: kicad_sch" in msg
        assert "got: kicad_pcb" in msg
        assert isinstance(err, KiCadToolsError)


class TestFileNotFoundError:
    """Tests for FileNotFoundError exception."""

    def test_file_not_found(self):
        """Test file not found error."""
        err = FileNotFoundError(
            "Schematic not found",
            context={"file": "missing.kicad_sch"},
            suggestions=["Check the file path"],
        )
        msg = str(err)
        assert "Schematic not found" in msg
        assert "file: missing.kicad_sch" in msg
        assert isinstance(err, KiCadToolsError)


class TestRoutingError:
    """Tests for RoutingError exception."""

    def test_routing_error(self):
        """Test routing error."""
        err = RoutingError(
            "Cannot route net",
            context={"net": "GND", "from": "U1.GND", "to": "U2.GND"},
            suggestions=["Increase clearance"],
        )
        msg = str(err)
        assert "Cannot route net" in msg
        assert "net: GND" in msg
        assert isinstance(err, KiCadToolsError)


class TestComponentError:
    """Tests for ComponentError exception."""

    def test_component_error(self):
        """Test component error."""
        err = ComponentError(
            "Symbol not found",
            context={"symbol": "LM7805", "library": "Regulator_Linear"},
        )
        msg = str(err)
        assert "Symbol not found" in msg
        assert "symbol: LM7805" in msg
        assert isinstance(err, KiCadToolsError)


class TestConfigurationError:
    """Tests for ConfigurationError exception."""

    def test_configuration_error(self):
        """Test configuration error."""
        err = ConfigurationError(
            "Unknown manufacturer",
            context={"manufacturer": "invalid", "available": ["jlcpcb", "pcbway"]},
            suggestions=["Use one of the available manufacturers"],
        )
        msg = str(err)
        assert "Unknown manufacturer" in msg
        assert "manufacturer: invalid" in msg
        assert isinstance(err, KiCadToolsError)


class TestExportError:
    """Tests for ExportError exception."""

    def test_export_error(self):
        """Test export error."""
        err = ExportError(
            "Gerber export failed",
            context={"output_dir": "/tmp/gerbers", "reason": "kicad-cli not found"},
            suggestions=["Install KiCad"],
        )
        msg = str(err)
        assert "Gerber export failed" in msg
        assert "output_dir: /tmp/gerbers" in msg
        assert isinstance(err, KiCadToolsError)


class TestExceptionHierarchy:
    """Test that exception hierarchy is correct."""

    def test_all_inherit_from_base(self):
        """Test all exceptions inherit from KiCadToolsError."""
        exceptions = [
            ParseError,
            ValidationError,
            FileFormatError,
            FileNotFoundError,
            RoutingError,
            ComponentError,
            ConfigurationError,
            ExportError,
        ]
        for exc_class in exceptions:
            assert issubclass(exc_class, KiCadToolsError)

    def test_can_catch_by_base_class(self):
        """Test that all exceptions can be caught by base class."""
        with pytest.raises(KiCadToolsError):
            raise ParseError("test")

        with pytest.raises(KiCadToolsError):
            raise ValidationError(["test"])

        with pytest.raises(KiCadToolsError):
            raise ConfigurationError("test")

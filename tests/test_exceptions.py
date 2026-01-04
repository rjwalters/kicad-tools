"""Tests for kicad_tools.exceptions module."""

import json
from pathlib import Path

import pytest

from kicad_tools.exceptions import (
    ComponentError,
    ConfigurationError,
    ExportError,
    FileFormatError,
    FileNotFoundError,
    KiCadDiagnostic,
    KiCadToolsError,
    ParseError,
    RoutingError,
    SourcePosition,
    ValidationError,
    _class_name_to_error_code,
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

    def test_error_code_auto_generated(self):
        """Test that error_code is auto-generated from class name."""
        err = KiCadToolsError("Test error")
        assert err.error_code == "KI_CAD_TOOLS"

    def test_error_code_explicit(self):
        """Test that explicit error_code overrides auto-generation."""
        err = KiCadToolsError("Test error", error_code="CUSTOM_CODE")
        assert err.error_code == "CUSTOM_CODE"

    def test_to_dict_basic(self):
        """Test to_dict returns correct structure."""
        err = KiCadToolsError("Test message")
        result = err.to_dict()
        assert result["error_code"] == "KI_CAD_TOOLS"
        assert result["message"] == "Test message"
        assert result["context"] == {}
        assert result["suggestions"] == []

    def test_to_dict_with_all_fields(self):
        """Test to_dict with all fields populated."""
        err = KiCadToolsError(
            "Full error",
            context={"key": "value"},
            suggestions=["Try this"],
            error_code="CUSTOM",
        )
        result = err.to_dict()
        assert result["error_code"] == "CUSTOM"
        assert result["message"] == "Full error"
        assert result["context"] == {"key": "value"}
        assert result["suggestions"] == ["Try this"]

    def test_to_dict_json_serializable(self):
        """Test that to_dict output is JSON-serializable."""
        err = KiCadToolsError(
            "Test",
            context={"file": "test.txt", "line": 42},
            suggestions=["Fix it", "Try again"],
        )
        # Should not raise
        json_str = json.dumps(err.to_dict())
        parsed = json.loads(json_str)
        assert parsed["message"] == "Test"
        assert parsed["context"]["line"] == 42


class TestClassNameToErrorCode:
    """Tests for the error code conversion helper."""

    def test_simple_class_name(self):
        """Test simple class name conversion."""
        assert _class_name_to_error_code("ParseError") == "PARSE"

    def test_multi_word_class_name(self):
        """Test multi-word class name conversion."""
        assert _class_name_to_error_code("FileNotFoundError") == "FILE_NOT_FOUND"

    def test_class_without_error_suffix(self):
        """Test class name without Error suffix."""
        assert _class_name_to_error_code("Configuration") == "CONFIGURATION"

    def test_complex_class_name(self):
        """Test complex class names."""
        assert _class_name_to_error_code("KiCadToolsError") == "KI_CAD_TOOLS"


class TestParseError:
    """Tests for ParseError exception."""

    def test_basic_parse_error(self):
        """Test basic parse error."""
        err = ParseError("Unexpected token")
        assert "Unexpected token" in str(err)
        assert isinstance(err, KiCadToolsError)

    def test_error_code(self):
        """Test ParseError has correct error code."""
        err = ParseError("Test")
        assert err.error_code == "PARSE"

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

    def test_error_code(self):
        """Test ValidationError has correct error code."""
        err = ValidationError(["Test"])
        assert err.error_code == "VALIDATION"

    def test_to_dict_includes_errors(self):
        """Test ValidationError to_dict includes individual errors."""
        errors = ["Error 1", "Error 2", "Error 3"]
        err = ValidationError(errors, context={"file": "test.json"})
        result = err.to_dict()
        assert result["error_code"] == "VALIDATION"
        assert result["errors"] == errors
        assert result["context"] == {"file": "test.json"}

    def test_to_dict_json_serializable(self):
        """Test ValidationError to_dict is JSON-serializable."""
        err = ValidationError(
            ["Missing field", "Invalid value"],
            context={"file": "data.json"},
            suggestions=["Check the schema"],
        )
        json_str = json.dumps(err.to_dict())
        parsed = json.loads(json_str)
        assert parsed["errors"] == ["Missing field", "Invalid value"]
        assert parsed["suggestions"] == ["Check the schema"]


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


class TestErrorCodes:
    """Test that all exception classes have correct error codes."""

    def test_all_exceptions_have_error_codes(self):
        """Test all exception classes have auto-generated error codes."""
        test_cases = [
            (ParseError("test"), "PARSE"),
            (ValidationError(["test"]), "VALIDATION"),
            (FileFormatError("test"), "FILE_FORMAT"),
            (FileNotFoundError("test"), "FILE_NOT_FOUND"),
            (RoutingError("test"), "ROUTING"),
            (ComponentError("test"), "COMPONENT"),
            (ConfigurationError("test"), "CONFIGURATION"),
            (ExportError("test"), "EXPORT"),
        ]
        for err, expected_code in test_cases:
            assert err.error_code == expected_code, f"{type(err).__name__} has wrong error code"

    def test_all_exceptions_have_to_dict(self):
        """Test all exception classes have to_dict method."""
        exceptions = [
            ParseError("test"),
            ValidationError(["test"]),
            FileFormatError("test"),
            FileNotFoundError("test"),
            RoutingError("test"),
            ComponentError("test"),
            ConfigurationError("test"),
            ExportError("test"),
        ]
        for err in exceptions:
            result = err.to_dict()
            assert "error_code" in result
            assert "message" in result
            assert "context" in result
            assert "suggestions" in result

    def test_error_codes_are_unique(self):
        """Test that each exception type has a unique error code."""
        exceptions = [
            ParseError("test"),
            ValidationError(["test"]),
            FileFormatError("test"),
            FileNotFoundError("test"),
            RoutingError("test"),
            ComponentError("test"),
            ConfigurationError("test"),
            ExportError("test"),
        ]
        codes = [err.error_code for err in exceptions]
        assert len(codes) == len(set(codes)), "Duplicate error codes found"

    def test_custom_error_code_override(self):
        """Test that custom error codes can be passed to subclasses."""
        err = FileNotFoundError("test", error_code="CUSTOM_NOT_FOUND")
        assert err.error_code == "CUSTOM_NOT_FOUND"

        err2 = ConfigurationError("test", error_code="CONFIG_INVALID")
        assert err2.error_code == "CONFIG_INVALID"


class TestSourcePosition:
    """Tests for the SourcePosition dataclass."""

    def test_basic_position(self):
        """Test basic source position creation."""
        pos = SourcePosition(
            file_path=Path("test.kicad_sch"),
            line=42,
            column=5,
        )
        assert pos.file_path == Path("test.kicad_sch")
        assert pos.line == 42
        assert pos.column == 5
        assert pos.element_type == ""
        assert pos.element_ref == ""
        assert pos.position_mm is None
        assert pos.layer is None

    def test_full_position(self):
        """Test source position with all fields."""
        pos = SourcePosition(
            file_path=Path("board.kicad_pcb"),
            line=100,
            column=10,
            element_type="track",
            element_ref="net-GND",
            position_mm=(25.4, 50.8),
            layer="F.Cu",
        )
        assert pos.element_type == "track"
        assert pos.element_ref == "net-GND"
        assert pos.position_mm == (25.4, 50.8)
        assert pos.layer == "F.Cu"

    def test_str_format(self):
        """Test string formatting as file:line:column."""
        pos = SourcePosition(
            file_path=Path("project.kicad_sch"),
            line=42,
            column=15,
        )
        assert str(pos) == "project.kicad_sch:42:15"

    def test_repr(self):
        """Test repr includes key fields."""
        pos = SourcePosition(
            file_path=Path("board.kicad_pcb"),
            line=10,
            column=5,
            element_type="symbol",
            element_ref="U1",
        )
        r = repr(pos)
        assert "SourcePosition" in r
        assert "board.kicad_pcb" in r
        assert "line=10" in r
        assert "column=5" in r
        assert "element_type='symbol'" in r
        assert "element_ref='U1'" in r

    def test_to_dict_basic(self):
        """Test to_dict returns correct structure."""
        pos = SourcePosition(
            file_path=Path("test.kicad_sch"),
            line=42,
            column=5,
        )
        result = pos.to_dict()
        assert result["file_path"] == "test.kicad_sch"
        assert result["line"] == 42
        assert result["column"] == 5
        assert "element_type" not in result  # Empty string not included
        assert "element_ref" not in result
        assert "position_mm" not in result
        assert "layer" not in result

    def test_to_dict_full(self):
        """Test to_dict with all fields."""
        pos = SourcePosition(
            file_path=Path("board.kicad_pcb"),
            line=100,
            column=10,
            element_type="via",
            element_ref="VIA-1",
            position_mm=(12.7, 25.4),
            layer="F.Cu",
        )
        result = pos.to_dict()
        assert result["element_type"] == "via"
        assert result["element_ref"] == "VIA-1"
        assert result["position_mm"] == {"x": 12.7, "y": 25.4}
        assert result["layer"] == "F.Cu"

    def test_to_dict_json_serializable(self):
        """Test to_dict output is JSON-serializable."""
        pos = SourcePosition(
            file_path=Path("test.kicad_pcb"),
            line=10,
            column=20,
            element_type="track",
            position_mm=(1.0, 2.0),
        )
        json_str = json.dumps(pos.to_dict())
        parsed = json.loads(json_str)
        assert parsed["line"] == 10
        assert parsed["position_mm"]["x"] == 1.0


class TestKiCadDiagnostic:
    """Tests for the KiCadDiagnostic exception class."""

    def test_basic_message(self):
        """Test basic diagnostic message without position."""
        err = KiCadDiagnostic("Something went wrong")
        assert str(err) == "Something went wrong"
        assert err.message == "Something went wrong"
        assert err.source is None
        assert err.sources == []
        assert err.suggestions == []

    def test_with_source_position(self):
        """Test diagnostic with source position."""
        pos = SourcePosition(
            file_path=Path("board.kicad_pcb"),
            line=42,
            column=5,
        )
        err = KiCadDiagnostic("Track clearance violation", source=pos)
        msg = str(err)
        assert "board.kicad_pcb:42:5: Track clearance violation" in msg
        assert err.source == pos
        assert err.location == "board.kicad_pcb:42:5"

    def test_with_suggestions(self):
        """Test diagnostic with suggestions."""
        err = KiCadDiagnostic(
            "Invalid spacing",
            suggestions=["Increase clearance", "Check design rules"],
        )
        msg = str(err)
        assert "Invalid spacing" in msg
        assert "Suggestions:" in msg
        assert "Increase clearance" in msg
        assert "Check design rules" in msg

    def test_with_related_sources(self):
        """Test diagnostic with multiple related positions."""
        primary = SourcePosition(
            file_path=Path("board.kicad_pcb"),
            line=42,
            column=5,
            element_ref="net-GND",
        )
        related1 = SourcePosition(
            file_path=Path("board.kicad_pcb"),
            line=100,
            column=10,
            element_ref="U1",
        )
        related2 = SourcePosition(
            file_path=Path("board.kicad_pcb"),
            line=150,
            column=15,
            element_ref="C1",
        )
        err = KiCadDiagnostic(
            "Clearance violation",
            source=primary,
            sources=[related1, related2],
        )
        msg = str(err)
        assert "board.kicad_pcb:42:5: Clearance violation" in msg
        assert "Related locations:" in msg
        assert "board.kicad_pcb:100:10 (U1)" in msg
        assert "board.kicad_pcb:150:15 (C1)" in msg

    def test_location_property(self):
        """Test location property."""
        # With source
        pos = SourcePosition(file_path=Path("test.kicad_sch"), line=10, column=5)
        err = KiCadDiagnostic("Test", source=pos)
        assert err.location == "test.kicad_sch:10:5"

        # Without source
        err2 = KiCadDiagnostic("Test")
        assert err2.location == ""

    def test_to_dict_basic(self):
        """Test to_dict returns correct structure."""
        err = KiCadDiagnostic("Test message")
        result = err.to_dict()
        assert result["message"] == "Test message"
        assert result["suggestions"] == []
        assert "source" not in result
        assert "related_sources" not in result

    def test_to_dict_with_source(self):
        """Test to_dict with source position."""
        pos = SourcePosition(
            file_path=Path("board.kicad_pcb"),
            line=42,
            column=5,
            element_type="track",
        )
        err = KiCadDiagnostic("Violation", source=pos, suggestions=["Fix it"])
        result = err.to_dict()
        assert result["message"] == "Violation"
        assert result["suggestions"] == ["Fix it"]
        assert result["source"]["file_path"] == "board.kicad_pcb"
        assert result["source"]["line"] == 42

    def test_to_dict_with_related_sources(self):
        """Test to_dict with related sources."""
        primary = SourcePosition(file_path=Path("a.kicad_pcb"), line=1, column=1)
        related = SourcePosition(file_path=Path("b.kicad_pcb"), line=2, column=2)
        err = KiCadDiagnostic("Test", source=primary, sources=[related])
        result = err.to_dict()
        assert len(result["related_sources"]) == 1
        assert result["related_sources"][0]["file_path"] == "b.kicad_pcb"

    def test_to_dict_json_serializable(self):
        """Test to_dict output is JSON-serializable."""
        pos = SourcePosition(
            file_path=Path("test.kicad_pcb"),
            line=10,
            column=5,
            element_type="via",
            position_mm=(1.0, 2.0),
        )
        err = KiCadDiagnostic(
            "Drill size violation",
            source=pos,
            suggestions=["Increase drill size"],
        )
        json_str = json.dumps(err.to_dict())
        parsed = json.loads(json_str)
        assert parsed["message"] == "Drill size violation"
        assert parsed["source"]["line"] == 10

    def test_is_exception(self):
        """Test KiCadDiagnostic is an Exception."""
        err = KiCadDiagnostic("Test")
        assert isinstance(err, Exception)

    def test_can_be_raised(self):
        """Test KiCadDiagnostic can be raised and caught."""
        with pytest.raises(KiCadDiagnostic) as exc_info:
            raise KiCadDiagnostic("Test error")
        assert exc_info.value.message == "Test error"

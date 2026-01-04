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
    KiCadToolsError,
    ParseError,
    RoutingError,
    SExpSnippetExtractor,
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


class TestSExpSnippetExtractor:
    """Tests for SExpSnippetExtractor class."""

    @pytest.fixture
    def sample_sexp_file(self, tmp_path):
        """Create a sample S-expression file for testing."""
        content = """\
(kicad_pcb
  (version 20231120)
  (generator "test")
  (footprint "Capacitor_SMD:C_0402"
    (at 45.2 32.1)
    (property "Reference" "C1"
      (at 0 -1.5 0)
    )
    (property "Value" "100nF"
      (at 0 1.5 0)
    )
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5))
  )
  (footprint "Resistor_SMD:R_0603"
    (at 50.0 32.1)
    (property "Reference" "R1"
      (at 0 -1.5 0)
    )
    (property "Value" "10k"
      (at 0 1.5 0)
    )
  )
)"""
        file_path = tmp_path / "test.kicad_pcb"
        file_path.write_text(content)
        return file_path

    def test_extract_basic(self, sample_sexp_file):
        """Test basic line extraction with default context."""
        extractor = SExpSnippetExtractor()
        snippet = extractor.extract(sample_sexp_file, line=5)

        # Should contain the target line
        assert "(at 45.2 32.1)" in snippet
        # Should have the arrow marker on line 5
        assert "->    5 |" in snippet
        # Should have context lines without markers
        assert "      4 |" in snippet
        assert "      6 |" in snippet

    def test_extract_with_custom_context(self, sample_sexp_file):
        """Test extraction with custom context lines."""
        extractor = SExpSnippetExtractor(context_lines=1)
        snippet = extractor.extract(sample_sexp_file, line=5)

        lines = snippet.strip().split("\n")
        # With context_lines=1, should have 3 lines (1 before, target, 1 after)
        assert len(lines) == 3

    def test_extract_override_context(self, sample_sexp_file):
        """Test that context_lines parameter overrides default."""
        extractor = SExpSnippetExtractor(context_lines=1)
        snippet = extractor.extract(sample_sexp_file, line=5, context_lines=5)

        lines = snippet.strip().split("\n")
        # Should use the override value
        assert len(lines) > 3

    def test_extract_first_line(self, sample_sexp_file):
        """Test extraction at the first line of the file."""
        extractor = SExpSnippetExtractor(context_lines=3)
        snippet = extractor.extract(sample_sexp_file, line=1)

        # Should start with line 1, no lines before
        assert "->    1 |" in snippet
        # Should have lines after
        assert "      2 |" in snippet

    def test_extract_last_line(self, sample_sexp_file):
        """Test extraction at the last line of the file."""
        content = sample_sexp_file.read_text()
        total_lines = len(content.splitlines())

        extractor = SExpSnippetExtractor(context_lines=3)
        snippet = extractor.extract(sample_sexp_file, line=total_lines)

        # Should have the arrow on the last line
        assert f"-> {total_lines:4d} |" in snippet

    def test_extract_custom_marker(self, sample_sexp_file):
        """Test extraction with custom marker."""
        extractor = SExpSnippetExtractor(marker=">>>")
        snippet = extractor.extract(sample_sexp_file, line=5)

        assert ">>>    5 |" in snippet

    def test_extract_line_numbers_displayed(self, sample_sexp_file):
        """Test that line numbers are correctly displayed."""
        extractor = SExpSnippetExtractor()
        snippet = extractor.extract(sample_sexp_file, line=5)

        # Check line numbers are present
        assert " 4 |" in snippet
        assert " 5 |" in snippet
        assert " 6 |" in snippet

    def test_extract_file_not_found(self, tmp_path):
        """Test extraction with non-existent file."""
        extractor = SExpSnippetExtractor()
        with pytest.raises(FileNotFoundError):
            extractor.extract(tmp_path / "nonexistent.kicad_pcb", line=5)

    def test_extract_line_out_of_range(self, sample_sexp_file):
        """Test extraction with line number out of range."""
        extractor = SExpSnippetExtractor()
        with pytest.raises(ValueError) as exc_info:
            extractor.extract(sample_sexp_file, line=1000)
        assert "out of range" in str(exc_info.value)

    def test_extract_line_zero(self, sample_sexp_file):
        """Test extraction with line 0 (invalid)."""
        extractor = SExpSnippetExtractor()
        with pytest.raises(ValueError):
            extractor.extract(sample_sexp_file, line=0)

    def test_extract_element_footprint(self, sample_sexp_file):
        """Test extracting footprint element by reference."""
        extractor = SExpSnippetExtractor()
        element = extractor.extract_element(sample_sexp_file, element_ref="C1")

        assert element is not None
        assert "footprint" in element
        assert "C_0402" in element
        assert "C1" in element

    def test_extract_element_another_ref(self, sample_sexp_file):
        """Test extracting a different footprint by reference."""
        extractor = SExpSnippetExtractor()
        element = extractor.extract_element(sample_sexp_file, element_ref="R1")

        assert element is not None
        assert "R_0603" in element
        assert "R1" in element

    def test_extract_element_not_found(self, sample_sexp_file):
        """Test extracting non-existent element."""
        extractor = SExpSnippetExtractor()
        element = extractor.extract_element(sample_sexp_file, element_ref="U99")

        assert element is None

    def test_extract_element_file_not_found(self, tmp_path):
        """Test element extraction with non-existent file."""
        extractor = SExpSnippetExtractor()
        with pytest.raises(FileNotFoundError):
            extractor.extract_element(tmp_path / "nonexistent.kicad_pcb", "C1")

    def test_extract_with_header(self, sample_sexp_file):
        """Test extract_with_header includes file path and line."""
        extractor = SExpSnippetExtractor()
        output = extractor.extract_with_header(
            sample_sexp_file,
            line=5,
            message="DRC Error: Clearance violation",
        )

        assert "DRC Error: Clearance violation" in output
        assert str(sample_sexp_file) in output
        assert ":5" in output
        assert "(at 45.2 32.1)" in output

    def test_extract_with_header_no_message(self, sample_sexp_file):
        """Test extract_with_header without message."""
        extractor = SExpSnippetExtractor()
        output = extractor.extract_with_header(sample_sexp_file, line=5)

        assert str(sample_sexp_file) in output
        assert "(at 45.2 32.1)" in output


class TestParseErrorSnippet:
    """Tests for ParseError snippet integration."""

    @pytest.fixture
    def sample_file(self, tmp_path):
        """Create a sample file for testing."""
        content = """\
line 1
line 2
line 3
line 4 - this is the error line
line 5
line 6
line 7"""
        file_path = tmp_path / "test.txt"
        file_path.write_text(content)
        return file_path

    def test_get_snippet(self, sample_file):
        """Test ParseError.get_snippet() method."""
        err = ParseError(
            "Syntax error",
            file_path=str(sample_file),
            line=4,
        )
        snippet = err.get_snippet()

        assert snippet is not None
        assert "line 4 - this is the error line" in snippet
        assert "->    4 |" in snippet

    def test_get_snippet_no_file(self):
        """Test get_snippet when no file in context."""
        err = ParseError("Syntax error")
        snippet = err.get_snippet()
        assert snippet is None

    def test_get_snippet_no_line(self, sample_file):
        """Test get_snippet when no line in context."""
        err = ParseError("Syntax error", file_path=str(sample_file))
        snippet = err.get_snippet()
        assert snippet is None

    def test_get_snippet_file_not_found(self, tmp_path):
        """Test get_snippet when file doesn't exist."""
        err = ParseError(
            "Syntax error",
            file_path=str(tmp_path / "nonexistent.txt"),
            line=4,
        )
        snippet = err.get_snippet()
        assert snippet is None

    def test_format_with_snippet(self, sample_file):
        """Test ParseError.format_with_snippet() method."""
        err = ParseError(
            "Unexpected token",
            file_path=str(sample_file),
            line=4,
            suggestions=["Check syntax", "Verify encoding"],
        )
        formatted = err.format_with_snippet()

        assert "Unexpected token" in formatted
        assert str(sample_file) in formatted
        assert ":4" in formatted
        assert "line 4 - this is the error line" in formatted
        assert "Suggestions:" in formatted
        assert "Check syntax" in formatted

    def test_format_with_snippet_no_context(self):
        """Test format_with_snippet when no file/line available."""
        err = ParseError("Syntax error")
        formatted = err.format_with_snippet()

        # Should just have the message
        assert "Syntax error" in formatted

    def test_format_with_snippet_custom_context_lines(self, sample_file):
        """Test format_with_snippet with custom context lines."""
        err = ParseError(
            "Error",
            file_path=str(sample_file),
            line=4,
        )
        formatted = err.format_with_snippet(context_lines=1)

        lines = formatted.strip().split("\n")
        # Should have message, file location, blank, and 3 snippet lines
        snippet_lines = [l for l in lines if "|" in l]
        assert len(snippet_lines) == 3  # 1 before, target, 1 after

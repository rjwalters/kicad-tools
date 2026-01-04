"""Tests for kicad_tools.exceptions module."""

import json
from pathlib import Path

import pytest

from kicad_tools.exceptions import (
    ComponentError,
    ConfigurationError,
    ErrorAccumulator,
    ExportError,
    FileFormatError,
    FileNotFoundError,
    KiCadDiagnostic,
    KiCadToolsError,
    ParseError,
    RoutingError,
    SExpSnippetExtractor,
    SourcePosition,
    ValidationError,
    ValidationErrorGroup,
    _class_name_to_error_code,
    accumulate,
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


class TestValidationErrorGroup:
    """Tests for ValidationErrorGroup exception."""

    def test_single_error_in_group(self):
        """Test group with single error."""
        inner = ValidationError(["Field required"])
        group = ValidationErrorGroup([inner])
        assert len(group.errors) == 1
        assert "1 validation error" in str(group)
        assert "Field required" in str(group)

    def test_multiple_errors_in_group(self):
        """Test group with multiple errors."""
        errors = [
            ValidationError(["Field 'name' required"]),
            ValidationError(["Invalid format"]),
            ParseError("Unexpected token"),
        ]
        group = ValidationErrorGroup(errors)
        msg = str(group)
        assert len(group.errors) == 3
        assert "3 validation errors" in msg
        assert "Field 'name' required" in msg
        assert "Invalid format" in msg
        assert "Unexpected token" in msg

    def test_error_code(self):
        """Test ValidationErrorGroup has correct error code."""
        group = ValidationErrorGroup([ValidationError(["test"])])
        assert group.error_code == "VALIDATION_ERROR_GROUP"

    def test_inherits_from_base(self):
        """Test ValidationErrorGroup inherits from KiCadToolsError."""
        group = ValidationErrorGroup([])
        assert isinstance(group, KiCadToolsError)

    def test_with_context_and_suggestions(self):
        """Test group with context and suggestions."""
        group = ValidationErrorGroup(
            [ValidationError(["Error"])],
            context={"file": "test.json"},
            suggestions=["Fix the errors"],
        )
        msg = str(group)
        assert "file: test.json" in msg
        assert "Fix the errors" in msg

    def test_to_dict_includes_all_errors(self):
        """Test to_dict includes all grouped errors."""
        errors = [
            ValidationError(["Error 1"]),
            ValidationError(["Error 2"]),
        ]
        group = ValidationErrorGroup(errors, context={"batch": "test"})
        result = group.to_dict()

        assert result["error_code"] == "VALIDATION_ERROR_GROUP"
        assert result["error_count"] == 2
        assert len(result["errors"]) == 2
        assert result["context"] == {"batch": "test"}

    def test_to_dict_with_mixed_exceptions(self):
        """Test to_dict handles both KiCadToolsError and other exceptions."""
        errors = [
            ValidationError(["Error 1"]),
            ValueError("Standard error"),
        ]
        group = ValidationErrorGroup(errors)
        result = group.to_dict()

        assert len(result["errors"]) == 2
        # First error is KiCadToolsError, should have full structure
        assert result["errors"][0]["error_code"] == "VALIDATION"
        # Second is ValueError, should have basic structure
        assert result["errors"][1]["error_code"] == "VALUEERROR"
        assert result["errors"][1]["message"] == "Standard error"

    def test_to_dict_json_serializable(self):
        """Test that to_dict output is JSON-serializable."""
        errors = [
            ValidationError(["Field missing"]),
            ParseError("Syntax error", context={"line": 10}),
        ]
        group = ValidationErrorGroup(errors)
        json_str = json.dumps(group.to_dict())
        parsed = json.loads(json_str)
        assert parsed["error_count"] == 2


class TestErrorAccumulator:
    """Tests for ErrorAccumulator class."""

    def test_empty_accumulator(self):
        """Test accumulator with no errors."""
        acc = ErrorAccumulator()
        assert not acc.has_errors()
        assert acc.error_count == 0
        assert acc.errors == []

    def test_collect_single_error(self):
        """Test collecting a single error."""
        acc = ErrorAccumulator(ValueError)

        with acc.collect():
            raise ValueError("test error")

        assert acc.has_errors()
        assert acc.error_count == 1
        assert isinstance(acc.errors[0], ValueError)
        assert str(acc.errors[0]) == "test error"

    def test_collect_multiple_errors(self):
        """Test collecting multiple errors in a loop."""
        acc = ErrorAccumulator(ValueError)

        values = [1, "bad", 3, "also bad", 5]
        for v in values:
            with acc.collect():
                if isinstance(v, str):
                    raise ValueError(f"Not a number: {v}")

        assert acc.error_count == 2
        assert "bad" in str(acc.errors[0])
        assert "also bad" in str(acc.errors[1])

    def test_collect_ignores_non_matching_exceptions(self):
        """Test that collect only catches specified exception type."""
        acc = ErrorAccumulator(ValueError)

        with pytest.raises(TypeError):
            with acc.collect():
                raise TypeError("This should propagate")

        assert acc.error_count == 0

    def test_collect_all_exceptions_when_no_type(self):
        """Test accumulator catches all exceptions when no type specified."""
        acc = ErrorAccumulator()

        with acc.collect():
            raise ValueError("first")

        with acc.collect():
            raise TypeError("second")

        assert acc.error_count == 2

    def test_add_error_manually(self):
        """Test manually adding errors."""
        acc = ErrorAccumulator()
        acc.add_error(ValueError("manual error"))
        assert acc.error_count == 1

    def test_raise_if_errors_raises_group(self):
        """Test raise_if_errors raises ValidationErrorGroup."""
        acc = ErrorAccumulator()
        acc.add_error(ValueError("error 1"))
        acc.add_error(ValueError("error 2"))

        with pytest.raises(ValidationErrorGroup) as exc_info:
            acc.raise_if_errors()

        assert len(exc_info.value.errors) == 2

    def test_raise_if_errors_with_context(self):
        """Test raise_if_errors passes context and suggestions."""
        acc = ErrorAccumulator()
        acc.add_error(ValueError("error"))

        with pytest.raises(ValidationErrorGroup) as exc_info:
            acc.raise_if_errors(
                context={"operation": "validation"},
                suggestions=["Check your input"],
            )

        assert exc_info.value.context == {"operation": "validation"}
        assert exc_info.value.suggestions == ["Check your input"]

    def test_raise_if_errors_no_errors(self):
        """Test raise_if_errors does nothing when no errors."""
        acc = ErrorAccumulator()
        # Should not raise
        acc.raise_if_errors()

    def test_clear(self):
        """Test clear removes all errors."""
        acc = ErrorAccumulator()
        acc.add_error(ValueError("error"))
        assert acc.has_errors()

        acc.clear()
        assert not acc.has_errors()
        assert acc.errors == []

    def test_set_error_type(self):
        """Test set_error_type changes the caught type."""
        acc = ErrorAccumulator()

        # Default catches everything
        with acc.collect():
            raise ValueError("caught")
        assert acc.error_count == 1

        acc.clear()
        acc.set_error_type(TypeError)

        # Now only catches TypeError
        with pytest.raises(ValueError):
            with acc.collect():
                raise ValueError("not caught")

        with acc.collect():
            raise TypeError("caught")

        assert acc.error_count == 1

    def test_collect_does_not_suppress_on_success(self):
        """Test that collect doesn't affect normal execution."""
        acc = ErrorAccumulator()
        result = None

        with acc.collect():
            result = 42  # No exception

        assert result == 42
        assert not acc.has_errors()


class TestAccumulateContextManager:
    """Tests for accumulate() convenience context manager."""

    def test_accumulate_with_no_errors(self):
        """Test accumulate with no errors raised."""
        # Should not raise
        with accumulate() as acc:
            pass

        assert not acc.has_errors()

    def test_accumulate_collects_and_raises(self):
        """Test accumulate collects errors and raises at end."""
        with pytest.raises(ValidationErrorGroup) as exc_info:
            with accumulate(ValueError) as acc:
                with acc.collect():
                    raise ValueError("error 1")
                with acc.collect():
                    raise ValueError("error 2")

        assert len(exc_info.value.errors) == 2

    def test_accumulate_with_error_type(self):
        """Test accumulate with specific error type."""
        with pytest.raises(ValidationErrorGroup):
            with accumulate(ValueError) as acc:
                with acc.collect():
                    raise ValueError("caught")

    def test_accumulate_propagates_unmatched_exceptions(self):
        """Test that unmatched exceptions propagate through accumulate."""
        with pytest.raises(TypeError):
            with accumulate(ValueError) as acc:
                with acc.collect():
                    raise TypeError("not caught")

    def test_real_world_validation_pattern(self):
        """Test the real-world pattern from the issue description."""

        def validate_item(item: dict) -> None:
            if "name" not in item:
                raise ValidationError(["Field 'name' is required"])
            if len(item.get("name", "")) < 2:
                raise ValidationError(["Name must be at least 2 characters"])

        items = [
            {"name": "valid"},
            {},  # Missing name
            {"name": "x"},  # Name too short
            {"name": "also valid"},
        ]

        with pytest.raises(ValidationErrorGroup) as exc_info:
            with accumulate(ValidationError) as acc:
                for item in items:
                    with acc.collect():
                        validate_item(item)

        # Should have collected 2 errors
        assert len(exc_info.value.errors) == 2

    def test_accumulate_json_output(self):
        """Test that accumulated errors produce valid JSON output."""
        errors_to_collect = [
            ValidationError(["Error 1"], context={"field": "name"}),
            ValidationError(["Error 2"], context={"field": "value"}),
        ]

        try:
            with accumulate(ValidationError) as acc:
                for err in errors_to_collect:
                    with acc.collect():
                        raise err
        except ValidationErrorGroup as group:
            result = group.to_dict()
            json_str = json.dumps(result)
            parsed = json.loads(json_str)

            assert parsed["error_count"] == 2
            assert len(parsed["errors"]) == 2


class TestErrorAccumulatorIntegration:
    """Integration tests for error accumulation patterns."""

    def test_drc_style_check_pattern(self):
        """Test pattern similar to DRC checking with multiple rules."""

        class DRCError(Exception):
            def __init__(self, rule: str, message: str):
                self.rule = rule
                super().__init__(f"[{rule}] {message}")

        def check_clearance(value: float) -> None:
            if value < 0.2:
                raise DRCError("clearance", f"Clearance {value}mm < 0.2mm minimum")

        def check_track_width(value: float) -> None:
            if value < 0.1:
                raise DRCError("track_width", f"Track {value}mm < 0.1mm minimum")

        measurements = [
            ("clearance", 0.15),  # Fail
            ("clearance", 0.25),  # Pass
            ("track_width", 0.08),  # Fail
            ("track_width", 0.12),  # Pass
        ]

        acc = ErrorAccumulator(DRCError)
        for check_type, value in measurements:
            with acc.collect():
                if check_type == "clearance":
                    check_clearance(value)
                else:
                    check_track_width(value)

        assert acc.error_count == 2
        assert "clearance" in str(acc.errors[0])
        assert "track_width" in str(acc.errors[1])

    def test_nested_accumulation(self):
        """Test that error accumulation works in nested contexts."""
        # Outer catches ValidationErrorGroup (result of inner accumulation)
        outer_acc = ErrorAccumulator(ValidationErrorGroup)

        def inner_validation():
            inner_acc = ErrorAccumulator(ValueError)
            with inner_acc.collect():
                raise ValueError("inner error 1")
            with inner_acc.collect():
                raise ValueError("inner error 2")
            inner_acc.raise_if_errors()

        with outer_acc.collect():
            inner_validation()

        # Outer should catch the ValidationErrorGroup from inner
        assert outer_acc.error_count == 1
        assert isinstance(outer_acc.errors[0], ValidationErrorGroup)
        # The inner group should contain the 2 ValueError exceptions
        assert len(outer_acc.errors[0].errors) == 2


class TestRichConsoleRendering:
    """Tests for Rich console rendering support."""

    def test_rich_console_method_exists(self):
        """Test that __rich_console__ method exists on base class."""
        err = KiCadToolsError("Test message")
        assert hasattr(err, "__rich_console__")
        assert callable(err.__rich_console__)

    def test_rich_console_renders_basic_error(self):
        """Test Rich rendering of basic error."""
        from io import StringIO

        from rich.console import Console

        err = KiCadToolsError("Test error message")
        output = StringIO()
        console = Console(file=output, force_terminal=True, width=80)
        console.print(err)
        result = output.getvalue()
        assert "Test error message" in result
        assert "KI_CAD_TOOLS" in result

    def test_rich_console_renders_context(self):
        """Test Rich rendering includes context."""
        from io import StringIO

        from rich.console import Console

        err = KiCadToolsError(
            "File error",
            context={"file": "test.kicad_sch", "line": 42},
        )
        output = StringIO()
        console = Console(file=output, force_terminal=True, width=80)
        console.print(err)
        result = output.getvalue()
        assert "File error" in result
        assert "file" in result
        assert "test.kicad_sch" in result
        assert "line" in result
        assert "42" in result

    def test_rich_console_renders_suggestions(self):
        """Test Rich rendering includes suggestions."""
        from io import StringIO

        from rich.console import Console

        err = KiCadToolsError(
            "Configuration error",
            suggestions=["Check your config file", "Verify settings"],
        )
        output = StringIO()
        console = Console(file=output, force_terminal=True, width=80)
        console.print(err)
        result = output.getvalue()
        assert "Configuration error" in result
        assert "Suggestions" in result
        assert "Check your config file" in result
        assert "Verify settings" in result

    def test_rich_console_renders_source_snippet(self):
        """Test Rich rendering includes source snippet when available."""
        from io import StringIO

        from rich.console import Console

        err = ParseError(
            "Syntax error",
            context={
                "file": "test.kicad_sch",
                "line": 3,
                "source_snippet": "(kicad_sch\n  (version 20230121)\n  (bad_token here)\n)",
                "highlight_line": 3,
            },
        )
        output = StringIO()
        console = Console(file=output, force_terminal=True, width=80)
        console.print(err)
        result = output.getvalue()
        assert "Syntax error" in result
        # Source snippet should be rendered
        assert "kicad_sch" in result or "bad_token" in result

    def test_validation_error_rich_console(self):
        """Test ValidationError has its own rich rendering."""
        from io import StringIO

        from rich.console import Console

        err = ValidationError(
            ["Missing field: name", "Invalid value: type"],
            context={"file": "config.json"},
        )
        output = StringIO()
        console = Console(file=output, force_terminal=True, width=80)
        console.print(err)
        result = output.getvalue()
        assert "2 error" in result
        assert "Missing field: name" in result
        assert "Invalid value: type" in result

    def test_plain_str_fallback_preserved(self):
        """Test that plain text __str__ still works for non-TTY."""
        err = KiCadToolsError(
            "Test error",
            context={"key": "value"},
            suggestions=["Try this"],
        )
        # __str__ should still return plain text
        plain = str(err)
        assert "Test error" in plain
        assert "Context:" in plain
        assert "key: value" in plain
        assert "Suggestions:" in plain
        assert "Try this" in plain

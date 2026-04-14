"""Tests for ERC violation classification (blocking vs non-blocking).

Tests the ERC_BLOCKING_TYPES and ERC_NON_BLOCKING_TYPES frozensets,
the ERCStatus.blocking_error_count field, and the verdict gate that
uses blocking_error_count instead of raw error_count.
"""

from pathlib import Path

import pytest

from kicad_tools.audit import AuditResult, AuditVerdict
from kicad_tools.audit.auditor import ERCStatus
from kicad_tools.erc import ERCReport, ERCViolationType
from kicad_tools.erc.violation import (
    ERC_BLOCKING_TYPES,
    ERC_NON_BLOCKING_TYPES,
)


class TestERCBlockingAndNonBlockingSets:
    """Tests for ERC_BLOCKING_TYPES and ERC_NON_BLOCKING_TYPES frozensets."""

    def test_blocking_types_is_frozenset(self):
        """ERC_BLOCKING_TYPES must be a frozenset."""
        assert isinstance(ERC_BLOCKING_TYPES, frozenset)

    def test_non_blocking_types_is_frozenset(self):
        """ERC_NON_BLOCKING_TYPES must be a frozenset."""
        assert isinstance(ERC_NON_BLOCKING_TYPES, frozenset)

    def test_blocking_and_non_blocking_are_disjoint(self):
        """Blocking and non-blocking sets must not overlap."""
        overlap = ERC_BLOCKING_TYPES & ERC_NON_BLOCKING_TYPES
        assert overlap == frozenset(), f"Overlap found: {overlap}"

    def test_blocking_types_contain_expected_electrical_errors(self):
        """Blocking types should include core electrical errors."""
        expected = {
            ERCViolationType.POWER_PIN_NOT_DRIVEN,
            ERCViolationType.PIN_NOT_CONNECTED,
            ERCViolationType.PIN_NOT_DRIVEN,
            ERCViolationType.DIFFERENT_UNIT_NET,
            ERCViolationType.DUPLICATE_REFERENCE,
            ERCViolationType.MISSING_POWER_PIN,
            ERCViolationType.HIER_LABEL_MISMATCH,
            ERCViolationType.BUS_TO_NET_CONFLICT,
            ERCViolationType.BUS_TO_BUS_CONFLICT,
        }
        assert expected == ERC_BLOCKING_TYPES

    def test_non_blocking_types_contain_expected_library_checks(self):
        """Non-blocking types should include library/footprint checks."""
        assert ERCViolationType.LIB_SYMBOL_MISMATCH in ERC_NON_BLOCKING_TYPES
        assert ERCViolationType.FOOTPRINT_LINK_ISSUES in ERC_NON_BLOCKING_TYPES
        assert ERCViolationType.SINGLE_GLOBAL_LABEL in ERC_NON_BLOCKING_TYPES
        assert ERCViolationType.ISOLATED_PIN_LABEL in ERC_NON_BLOCKING_TYPES
        assert ERCViolationType.PIN_TO_PIN in ERC_NON_BLOCKING_TYPES
        assert ERCViolationType.ENDPOINT_OFF_GRID in ERC_NON_BLOCKING_TYPES
        assert ERCViolationType.SIMILAR_LABELS in ERC_NON_BLOCKING_TYPES

    def test_all_set_members_are_valid_enum_values(self):
        """Every member of both sets must be a valid ERCViolationType value."""
        for t in ERC_BLOCKING_TYPES:
            assert isinstance(t, ERCViolationType), f"{t} is not ERCViolationType"
        for t in ERC_NON_BLOCKING_TYPES:
            assert isinstance(t, ERCViolationType), f"{t} is not ERCViolationType"

    def test_unknown_type_is_not_in_either_set(self):
        """UNKNOWN type should not be in either set (defaults to blocking behavior)."""
        assert ERCViolationType.UNKNOWN not in ERC_BLOCKING_TYPES
        assert ERCViolationType.UNKNOWN not in ERC_NON_BLOCKING_TYPES


class TestNewERCViolationTypes:
    """Tests for newly added ERCViolationType enum values."""

    def test_lib_symbol_mismatch_parses(self):
        """lib_symbol_mismatch should parse to LIB_SYMBOL_MISMATCH, not UNKNOWN."""
        assert (
            ERCViolationType.from_string("lib_symbol_mismatch")
            == ERCViolationType.LIB_SYMBOL_MISMATCH
        )

    def test_footprint_link_issues_parses(self):
        """footprint_link_issues should parse to FOOTPRINT_LINK_ISSUES, not UNKNOWN."""
        assert (
            ERCViolationType.from_string("footprint_link_issues")
            == ERCViolationType.FOOTPRINT_LINK_ISSUES
        )

    def test_single_global_label_parses(self):
        """single_global_label should parse to SINGLE_GLOBAL_LABEL, not UNKNOWN."""
        assert (
            ERCViolationType.from_string("single_global_label")
            == ERCViolationType.SINGLE_GLOBAL_LABEL
        )

    def test_isolated_pin_label_parses(self):
        """isolated_pin_label should parse to ISOLATED_PIN_LABEL, not UNKNOWN."""
        assert (
            ERCViolationType.from_string("isolated_pin_label")
            == ERCViolationType.ISOLATED_PIN_LABEL
        )

    def test_pin_to_pin_parses(self):
        """pin_to_pin should parse to PIN_TO_PIN, not UNKNOWN."""
        assert ERCViolationType.from_string("pin_to_pin") == ERCViolationType.PIN_TO_PIN


class TestNonBlockingERCFixture:
    """Tests using the non-blocking ERC fixture file."""

    @pytest.fixture
    def non_blocking_report(self, fixtures_dir: Path) -> ERCReport:
        """Load the non-blocking ERC fixture."""
        return ERCReport.load(fixtures_dir / "sample_erc_non_blocking.json")

    def test_non_blocking_fixture_loads(self, non_blocking_report: ERCReport):
        """Fixture should load without errors."""
        assert non_blocking_report.source_file == "test-non-blocking.kicad_sch"

    def test_non_blocking_fixture_has_errors(self, non_blocking_report: ERCReport):
        """Fixture should have error-severity violations."""
        assert non_blocking_report.error_count == 2  # lib_symbol_mismatch + footprint_link_issues

    def test_lib_symbol_mismatch_parsed_correctly(self, non_blocking_report: ERCReport):
        """lib_symbol_mismatch violations should parse to the correct type."""
        by_type = non_blocking_report.violations_by_type()
        assert ERCViolationType.LIB_SYMBOL_MISMATCH in by_type
        assert len(by_type[ERCViolationType.LIB_SYMBOL_MISMATCH]) == 1

    def test_footprint_link_issues_parsed_correctly(self, non_blocking_report: ERCReport):
        """footprint_link_issues violations should parse to the correct type."""
        by_type = non_blocking_report.violations_by_type()
        assert ERCViolationType.FOOTPRINT_LINK_ISSUES in by_type
        assert len(by_type[ERCViolationType.FOOTPRINT_LINK_ISSUES]) == 1

    def test_all_errors_are_non_blocking(self, non_blocking_report: ERCReport):
        """All error-level violations in the fixture should be non-blocking."""
        for v in non_blocking_report.errors:
            assert v.type in ERC_NON_BLOCKING_TYPES, (
                f"Error type {v.type} is not in ERC_NON_BLOCKING_TYPES"
            )


class TestERCStatusBlockingErrorCount:
    """Tests for ERCStatus.blocking_error_count field."""

    def test_default_blocking_error_count_is_zero(self):
        """ERCStatus should default blocking_error_count to 0."""
        status = ERCStatus()
        assert status.blocking_error_count == 0

    def test_blocking_error_count_in_to_dict(self):
        """blocking_error_count should appear in to_dict output."""
        status = ERCStatus(error_count=5, blocking_error_count=3)
        d = status.to_dict()
        assert "blocking_error_count" in d
        assert d["blocking_error_count"] == 3

    def test_error_count_preserved_raw(self):
        """error_count should remain the raw total, not affected by blocking split."""
        status = ERCStatus(error_count=5, blocking_error_count=2, warning_count=3)
        assert status.error_count == 5
        assert status.blocking_error_count == 2


class TestVerdictWithBlockingClassification:
    """Tests for AuditResult.verdict using the blocking_error_count gate."""

    def test_non_blocking_erc_error_does_not_block_ready(self):
        """A design with only non-blocking ERC errors should produce READY."""
        result = AuditResult()
        result.erc = ERCStatus(
            error_count=2,  # 2 raw errors (e.g., lib_symbol_mismatch)
            blocking_error_count=0,  # none are blocking
            warning_count=2,  # non-blocking errors demoted to warnings
            passed=True,
        )
        # Non-blocking errors are demoted to warnings, so verdict is WARNING
        assert result.verdict == AuditVerdict.WARNING

    def test_non_blocking_erc_only_with_zero_warnings_is_ready(self):
        """A design with non-blocking ERC errors but zero total warnings."""
        result = AuditResult()
        result.erc = ERCStatus(
            error_count=2,
            blocking_error_count=0,
            warning_count=0,  # non-blocking errors not counted as warnings here
            passed=True,
        )
        assert result.verdict == AuditVerdict.READY

    def test_blocking_erc_error_blocks_ready(self):
        """A design with blocking ERC errors should produce NOT_READY."""
        result = AuditResult()
        result.erc = ERCStatus(
            error_count=1,
            blocking_error_count=1,  # power_pin_not_driven
            warning_count=0,
            passed=False,
        )
        assert result.verdict == AuditVerdict.NOT_READY

    def test_unknown_erc_type_blocks_ready(self):
        """Unknown ERC violation types should block READY (conservative default)."""
        result = AuditResult()
        # Simulates the case where an unknown error type is treated as blocking
        result.erc = ERCStatus(
            error_count=1,
            blocking_error_count=1,  # unknown type defaults to blocking
            warning_count=0,
            passed=False,
        )
        assert result.verdict == AuditVerdict.NOT_READY

    def test_non_blocking_erc_errors_demoted_to_warnings(self):
        """Non-blocking errors should appear in warning_count."""
        result = AuditResult()
        result.erc = ERCStatus(
            error_count=3,
            blocking_error_count=0,
            warning_count=3,  # 3 non-blocking errors demoted to warnings
            passed=True,
        )
        # No blocking errors, but warnings present -> WARNING verdict
        assert result.verdict == AuditVerdict.WARNING
        assert result.is_ready is False

    def test_mix_of_blocking_and_non_blocking(self):
        """A mix of blocking + non-blocking errors should produce NOT_READY."""
        result = AuditResult()
        result.erc = ERCStatus(
            error_count=5,  # 2 blocking + 3 non-blocking
            blocking_error_count=2,
            warning_count=3,  # 3 non-blocking demoted
            passed=False,
        )
        assert result.verdict == AuditVerdict.NOT_READY

    def test_raw_error_count_does_not_affect_verdict(self):
        """error_count alone should NOT cause NOT_READY; only blocking_error_count matters."""
        result = AuditResult()
        result.erc = ERCStatus(
            error_count=100,  # lots of raw errors
            blocking_error_count=0,  # but none are blocking
            warning_count=0,
            passed=True,
        )
        assert result.verdict == AuditVerdict.READY

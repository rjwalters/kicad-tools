"""Tests for unified validation models."""

import pytest

from kicad_tools.validate.models import (
    BaseViolation,
    DRCResult,
    DRCViolation,
    Location,
    Severity,
    ValidationResult,
    ViolationCategory,
)


class TestSeverity:
    """Tests for Severity enum."""

    def test_severity_values(self):
        assert Severity.ERROR.value == "error"
        assert Severity.WARNING.value == "warning"
        assert Severity.INFO.value == "info"

    def test_severity_is_string_enum(self):
        # str enum allows direct string comparison
        assert Severity.ERROR == "error"
        assert Severity.WARNING == "warning"


class TestViolationCategory:
    """Tests for ViolationCategory enum."""

    def test_core_categories(self):
        assert ViolationCategory.DRC.value == "drc"
        assert ViolationCategory.CONNECTIVITY.value == "connectivity"
        assert ViolationCategory.CONSISTENCY.value == "consistency"
        assert ViolationCategory.NETLIST.value == "netlist"

    def test_future_categories(self):
        # Future categories are defined but not yet used
        assert ViolationCategory.THERMAL.value == "thermal"
        assert ViolationCategory.IMPEDANCE.value == "impedance"


class TestLocation:
    """Tests for Location model."""

    def test_empty_location(self):
        loc = Location()
        assert loc.x is None
        assert loc.y is None
        assert loc.to_tuple() is None

    def test_coordinate_location(self):
        loc = Location(x=10.5, y=20.3)
        assert loc.x == 10.5
        assert loc.y == 20.3
        assert loc.to_tuple() == (10.5, 20.3)

    def test_partial_coordinates(self):
        loc = Location(x=10.5)
        assert loc.to_tuple() is None

    def test_full_location(self):
        loc = Location(x=10.0, y=20.0, layer="F.Cu", ref="R1", net="VCC")
        assert loc.layer == "F.Cu"
        assert loc.ref == "R1"
        assert loc.net == "VCC"

    def test_location_is_frozen(self):
        from pydantic import ValidationError

        loc = Location(x=10.0, y=20.0)
        with pytest.raises(ValidationError):  # Frozen model raises ValidationError
            loc.x = 15.0  # type: ignore


class TestBaseViolation:
    """Tests for BaseViolation model."""

    def test_create_error_violation(self):
        v = BaseViolation(
            severity=Severity.ERROR,
            category=ViolationCategory.DRC,
            message="Test error",
        )
        assert v.is_error is True
        assert v.is_warning is False
        assert v.is_info is False

    def test_create_warning_violation(self):
        v = BaseViolation(
            severity=Severity.WARNING,
            category=ViolationCategory.CONNECTIVITY,
            message="Test warning",
        )
        assert v.is_error is False
        assert v.is_warning is True

    def test_create_info_violation(self):
        v = BaseViolation(
            severity=Severity.INFO,
            category=ViolationCategory.CONSISTENCY,
            message="Test info",
        )
        assert v.is_info is True

    def test_violation_with_location(self):
        loc = Location(x=10.0, y=20.0, layer="B.Cu")
        v = BaseViolation(
            severity=Severity.ERROR,
            category=ViolationCategory.DRC,
            message="Error at location",
            location=loc,
        )
        assert v.location is not None
        assert v.location.x == 10.0
        assert v.location.layer == "B.Cu"

    def test_to_dict(self):
        v = BaseViolation(
            severity=Severity.ERROR,
            category=ViolationCategory.DRC,
            message="Test",
            location=Location(x=1.0, y=2.0),
        )
        d = v.to_dict()
        assert d["severity"] == "error"
        assert d["category"] == "drc"
        assert d["message"] == "Test"
        assert d["location"]["x"] == 1.0

    def test_violation_is_frozen(self):
        from pydantic import ValidationError

        v = BaseViolation(
            severity=Severity.ERROR,
            category=ViolationCategory.DRC,
            message="Test",
        )
        with pytest.raises(ValidationError):  # Frozen model raises ValidationError
            v.message = "Changed"  # type: ignore


class TestValidationResult:
    """Tests for generic ValidationResult."""

    def test_empty_result(self):
        result: ValidationResult[BaseViolation] = ValidationResult()
        assert len(result) == 0
        assert result.error_count == 0
        assert result.warning_count == 0
        assert result.passed is True
        assert bool(result) is False

    def test_result_with_errors(self):
        result: ValidationResult[BaseViolation] = ValidationResult(
            violations=[
                BaseViolation(
                    severity=Severity.ERROR,
                    category=ViolationCategory.DRC,
                    message="Error 1",
                ),
                BaseViolation(
                    severity=Severity.ERROR,
                    category=ViolationCategory.DRC,
                    message="Error 2",
                ),
            ]
        )
        assert result.error_count == 2
        assert result.passed is False
        assert len(result.errors()) == 2

    def test_result_with_warnings_only(self):
        result: ValidationResult[BaseViolation] = ValidationResult(
            violations=[
                BaseViolation(
                    severity=Severity.WARNING,
                    category=ViolationCategory.CONNECTIVITY,
                    message="Warning 1",
                ),
            ]
        )
        assert result.warning_count == 1
        assert result.error_count == 0
        assert result.passed is True  # Warnings don't fail

    def test_add_violation(self):
        result: ValidationResult[BaseViolation] = ValidationResult()
        result.add(
            BaseViolation(
                severity=Severity.ERROR,
                category=ViolationCategory.DRC,
                message="Added",
            )
        )
        assert len(result) == 1

    def test_merge_results(self):
        result1: ValidationResult[BaseViolation] = ValidationResult(
            violations=[
                BaseViolation(
                    severity=Severity.ERROR,
                    category=ViolationCategory.DRC,
                    message="R1",
                )
            ]
        )
        result2: ValidationResult[BaseViolation] = ValidationResult(
            violations=[
                BaseViolation(
                    severity=Severity.WARNING,
                    category=ViolationCategory.DRC,
                    message="R2",
                )
            ]
        )
        result1.merge(result2)
        assert len(result1) == 2
        assert result1.error_count == 1
        assert result1.warning_count == 1

    def test_iteration(self):
        violations = [
            BaseViolation(
                severity=Severity.ERROR,
                category=ViolationCategory.DRC,
                message=f"V{i}",
            )
            for i in range(3)
        ]
        result: ValidationResult[BaseViolation] = ValidationResult(violations=violations)
        collected = list(result)
        assert len(collected) == 3

    def test_filter_by_category(self):
        result: ValidationResult[BaseViolation] = ValidationResult(
            violations=[
                BaseViolation(
                    severity=Severity.ERROR,
                    category=ViolationCategory.DRC,
                    message="DRC",
                ),
                BaseViolation(
                    severity=Severity.ERROR,
                    category=ViolationCategory.CONNECTIVITY,
                    message="Conn",
                ),
            ]
        )
        drc_only = result.filter_by_category(ViolationCategory.DRC)
        assert len(drc_only) == 1
        assert drc_only[0].message == "DRC"

    def test_to_dict(self):
        result: ValidationResult[BaseViolation] = ValidationResult(
            violations=[
                BaseViolation(
                    severity=Severity.ERROR,
                    category=ViolationCategory.DRC,
                    message="Test",
                )
            ]
        )
        d = result.to_dict()
        assert d["passed"] is False
        assert d["error_count"] == 1
        assert d["warning_count"] == 0
        assert len(d["violations"]) == 1


class TestDRCViolation:
    """Tests for DRC-specific violation model."""

    def test_create_drc_violation(self):
        v = DRCViolation(
            severity=Severity.ERROR,
            rule_id="clearance_trace_trace",
            message="Clearance violation",
            layer="F.Cu",
            actual_value=0.15,
            required_value=0.2,
            items=("D1", "C5"),
        )
        assert v.category == ViolationCategory.DRC
        assert v.rule_id == "clearance_trace_trace"
        assert v.is_error is True

    def test_drc_violation_to_dict_legacy_format(self):
        v = DRCViolation(
            severity=Severity.ERROR,
            rule_id="clearance",
            message="Test",
            location=Location(x=10.0, y=20.0),
            layer="F.Cu",
            actual_value=0.1,
            required_value=0.2,
            items=("R1", "R2"),
        )
        d = v.to_dict()
        # Verify legacy format compatibility
        assert d["rule_id"] == "clearance"
        assert d["severity"] == "error"
        assert d["location"] == [10.0, 20.0]  # List, not tuple
        assert d["items"] == ["R1", "R2"]  # List, not tuple
        assert d["actual_value"] == 0.1
        assert d["required_value"] == 0.2

    def test_drc_violation_no_location(self):
        v = DRCViolation(
            severity=Severity.WARNING,
            rule_id="test",
            message="No location",
        )
        d = v.to_dict()
        assert d["location"] is None


class TestDRCResult:
    """Tests for DRC-specific result model."""

    def test_empty_drc_result(self):
        result = DRCResult()
        assert result.passed is True
        assert result.rules_checked == 0
        assert len(result) == 0

    def test_drc_result_with_violations(self):
        result = DRCResult(
            violations=[
                DRCViolation(
                    severity=Severity.ERROR,
                    rule_id="clearance",
                    message="Clearance error",
                    layer="F.Cu",
                ),
                DRCViolation(
                    severity=Severity.WARNING,
                    rule_id="silkscreen",
                    message="Silkscreen warning",
                ),
            ],
            rules_checked=5,
        )
        assert result.error_count == 1
        assert result.warning_count == 1
        assert result.passed is False
        assert result.rules_checked == 5

    def test_filter_by_rule(self):
        result = DRCResult(
            violations=[
                DRCViolation(severity=Severity.ERROR, rule_id="clearance", message="C1"),
                DRCViolation(severity=Severity.ERROR, rule_id="clearance", message="C2"),
                DRCViolation(severity=Severity.ERROR, rule_id="trace_width", message="T1"),
            ]
        )
        clearance = result.filter_by_rule("clearance")
        assert len(clearance) == 2

    def test_filter_by_layer(self):
        result = DRCResult(
            violations=[
                DRCViolation(
                    severity=Severity.ERROR,
                    rule_id="test",
                    message="F",
                    layer="F.Cu",
                ),
                DRCViolation(
                    severity=Severity.ERROR,
                    rule_id="test",
                    message="B",
                    layer="B.Cu",
                ),
            ]
        )
        front = result.filter_by_layer("F.Cu")
        assert len(front) == 1
        assert front[0].message == "F"

    def test_merge_drc_results(self):
        r1 = DRCResult(
            violations=[DRCViolation(severity=Severity.ERROR, rule_id="r1", message="V1")],
            rules_checked=3,
        )
        r2 = DRCResult(
            violations=[DRCViolation(severity=Severity.WARNING, rule_id="r2", message="V2")],
            rules_checked=2,
        )
        r1.merge(r2)
        assert len(r1) == 2
        assert r1.rules_checked == 5

    def test_to_dict_legacy_format(self):
        result = DRCResult(
            violations=[DRCViolation(severity=Severity.ERROR, rule_id="test", message="E")],
            rules_checked=10,
        )
        d = result.to_dict()
        assert d["passed"] is False
        assert d["error_count"] == 1
        assert d["warning_count"] == 0
        assert d["rules_checked"] == 10
        assert len(d["violations"]) == 1

    def test_summary(self):
        result = DRCResult(
            violations=[
                DRCViolation(severity=Severity.ERROR, rule_id="test", message="E"),
                DRCViolation(severity=Severity.WARNING, rule_id="test", message="W"),
            ],
            rules_checked=5,
        )
        summary = result.summary()
        assert "FAILED" in summary
        assert "1 errors" in summary
        assert "1 warnings" in summary
        assert "5 rules checked" in summary

    def test_summary_passed(self):
        result = DRCResult(
            violations=[DRCViolation(severity=Severity.WARNING, rule_id="test", message="W")],
            rules_checked=3,
        )
        summary = result.summary()
        assert "PASSED" in summary


class TestBackwardsCompatibility:
    """Tests ensuring backwards compatibility with legacy violations.py."""

    def test_drc_violation_matches_legacy_to_dict(self):
        """Verify new DRCViolation.to_dict() matches legacy format."""
        v = DRCViolation(
            severity=Severity.ERROR,
            rule_id="clearance_trace_trace",
            message="Trace clearance violation",
            location=Location(x=100.5, y=200.3),
            layer="F.Cu",
            actual_value=0.15,
            required_value=0.2,
            items=("D1", "C5"),
        )
        d = v.to_dict()

        # Legacy format expectations
        assert isinstance(d["location"], list)  # Not tuple
        assert isinstance(d["items"], list)  # Not tuple
        assert d["severity"] == "error"  # String, not enum
        assert "category" not in d  # Legacy didn't have category

    def test_drc_result_matches_legacy_to_dict(self):
        """Verify new DRCResult.to_dict() matches legacy format."""
        result = DRCResult(
            violations=[DRCViolation(severity=Severity.ERROR, rule_id="test", message="Test")],
            rules_checked=5,
        )
        d = result.to_dict()

        # Legacy format expectations
        assert "passed" in d
        assert "error_count" in d
        assert "warning_count" in d
        assert "rules_checked" in d
        assert "violations" in d

"""Tests for kicad_tools.validate module (pure Python DRC)."""

from pathlib import Path

import pytest

from kicad_tools.validate import DRCChecker, DRCResults, DRCViolation


class TestDRCViolation:
    """Tests for the DRCViolation dataclass."""

    def test_violation_creation(self):
        """Test creating a basic violation."""
        v = DRCViolation(
            rule_id="clearance_trace_trace",
            severity="error",
            message="Trace to trace clearance 0.10mm < minimum 0.15mm",
        )
        assert v.rule_id == "clearance_trace_trace"
        assert v.severity == "error"
        assert v.message == "Trace to trace clearance 0.10mm < minimum 0.15mm"

    def test_violation_with_location(self):
        """Test violation with location coordinates."""
        v = DRCViolation(
            rule_id="clearance_trace_pad",
            severity="error",
            message="Trace too close to pad",
            location=(45.2, 30.1),
            layer="F.Cu",
        )
        assert v.location == (45.2, 30.1)
        assert v.layer == "F.Cu"

    def test_violation_with_values(self):
        """Test violation with actual and required values."""
        v = DRCViolation(
            rule_id="trace_width",
            severity="error",
            message="Trace width too small",
            actual_value=0.10,
            required_value=0.15,
        )
        assert v.actual_value == pytest.approx(0.10)
        assert v.required_value == pytest.approx(0.15)

    def test_violation_with_items(self):
        """Test violation with item references."""
        v = DRCViolation(
            rule_id="clearance_pad_pad",
            severity="error",
            message="Pad clearance violation",
            items=("U1-pad3", "C5-pad1"),
        )
        assert v.items == ("U1-pad3", "C5-pad1")

    def test_violation_is_hashable(self):
        """Test that violations are hashable (frozen dataclass)."""
        v = DRCViolation(
            rule_id="test",
            severity="error",
            message="test message",
        )
        # Should not raise
        hash(v)
        # Should work in sets
        violations = {v, v}
        assert len(violations) == 1

    def test_violation_is_error(self):
        """Test is_error property."""
        error = DRCViolation(rule_id="test", severity="error", message="test")
        warning = DRCViolation(rule_id="test", severity="warning", message="test")

        assert error.is_error is True
        assert error.is_warning is False
        assert warning.is_error is False
        assert warning.is_warning is True

    def test_violation_invalid_severity(self):
        """Test that invalid severity raises ValueError."""
        with pytest.raises(ValueError, match="severity must be"):
            DRCViolation(rule_id="test", severity="info", message="test")

    def test_violation_to_dict(self):
        """Test converting violation to dictionary."""
        v = DRCViolation(
            rule_id="clearance_trace_trace",
            severity="error",
            message="Test message",
            location=(10.0, 20.0),
            layer="F.Cu",
            actual_value=0.10,
            required_value=0.15,
            items=("A", "B"),
        )
        d = v.to_dict()

        assert d["rule_id"] == "clearance_trace_trace"
        assert d["severity"] == "error"
        assert d["message"] == "Test message"
        assert d["location"] == [10.0, 20.0]
        assert d["layer"] == "F.Cu"
        assert d["actual_value"] == pytest.approx(0.10)
        assert d["required_value"] == pytest.approx(0.15)
        assert d["items"] == ["A", "B"]


class TestDRCResults:
    """Tests for the DRCResults class."""

    def test_empty_results(self):
        """Test empty results."""
        results = DRCResults()
        assert results.error_count == 0
        assert results.warning_count == 0
        assert results.passed is True
        assert len(results) == 0
        assert bool(results) is False

    def test_error_count(self):
        """Test counting errors."""
        results = DRCResults(
            violations=[
                DRCViolation(rule_id="test1", severity="error", message="error 1"),
                DRCViolation(rule_id="test2", severity="error", message="error 2"),
                DRCViolation(rule_id="test3", severity="warning", message="warning 1"),
            ]
        )
        assert results.error_count == 2

    def test_warning_count(self):
        """Test counting warnings."""
        results = DRCResults(
            violations=[
                DRCViolation(rule_id="test1", severity="error", message="error 1"),
                DRCViolation(rule_id="test2", severity="warning", message="warning 1"),
                DRCViolation(rule_id="test3", severity="warning", message="warning 2"),
            ]
        )
        assert results.warning_count == 2

    def test_passed_with_errors(self):
        """Test passed is False when errors exist."""
        results = DRCResults(
            violations=[
                DRCViolation(rule_id="test", severity="error", message="error"),
            ]
        )
        assert results.passed is False

    def test_passed_with_only_warnings(self):
        """Test passed is True when only warnings exist."""
        results = DRCResults(
            violations=[
                DRCViolation(rule_id="test", severity="warning", message="warning"),
            ]
        )
        assert results.passed is True

    def test_iteration(self):
        """Test iterating over results."""
        v1 = DRCViolation(rule_id="test1", severity="error", message="error 1")
        v2 = DRCViolation(rule_id="test2", severity="warning", message="warning 1")
        results = DRCResults(violations=[v1, v2])

        violations = list(results)
        assert violations == [v1, v2]

    def test_add_violation(self):
        """Test adding a violation."""
        results = DRCResults()
        v = DRCViolation(rule_id="test", severity="error", message="error")
        results.add(v)

        assert len(results) == 1
        assert results.violations[0] == v

    def test_merge_results(self):
        """Test merging results from another DRCResults."""
        r1 = DRCResults(
            violations=[DRCViolation(rule_id="t1", severity="error", message="e1")],
            rules_checked=5,
        )
        r2 = DRCResults(
            violations=[DRCViolation(rule_id="t2", severity="warning", message="w1")],
            rules_checked=3,
        )

        r1.merge(r2)

        assert len(r1) == 2
        assert r1.rules_checked == 8

    def test_filter_by_rule(self):
        """Test filtering violations by rule ID."""
        results = DRCResults(
            violations=[
                DRCViolation(rule_id="clearance", severity="error", message="e1"),
                DRCViolation(rule_id="clearance", severity="error", message="e2"),
                DRCViolation(rule_id="dimension", severity="error", message="e3"),
            ]
        )

        clearance_violations = results.filter_by_rule("clearance")
        assert len(clearance_violations) == 2

    def test_filter_by_layer(self):
        """Test filtering violations by layer."""
        results = DRCResults(
            violations=[
                DRCViolation(rule_id="t1", severity="error", message="e1", layer="F.Cu"),
                DRCViolation(rule_id="t2", severity="error", message="e2", layer="B.Cu"),
                DRCViolation(rule_id="t3", severity="error", message="e3", layer="F.Cu"),
            ]
        )

        front_violations = results.filter_by_layer("F.Cu")
        assert len(front_violations) == 2

    def test_errors_property(self):
        """Test errors property returns only errors."""
        results = DRCResults(
            violations=[
                DRCViolation(rule_id="t1", severity="error", message="e1"),
                DRCViolation(rule_id="t2", severity="warning", message="w1"),
                DRCViolation(rule_id="t3", severity="error", message="e2"),
            ]
        )

        errors = results.errors
        assert len(errors) == 2
        assert all(e.is_error for e in errors)

    def test_warnings_property(self):
        """Test warnings property returns only warnings."""
        results = DRCResults(
            violations=[
                DRCViolation(rule_id="t1", severity="error", message="e1"),
                DRCViolation(rule_id="t2", severity="warning", message="w1"),
            ]
        )

        warnings = results.warnings
        assert len(warnings) == 1
        assert all(w.is_warning for w in warnings)

    def test_to_dict(self):
        """Test converting results to dictionary."""
        results = DRCResults(
            violations=[
                DRCViolation(rule_id="test", severity="error", message="error"),
            ],
            rules_checked=10,
        )
        d = results.to_dict()

        assert d["passed"] is False
        assert d["error_count"] == 1
        assert d["warning_count"] == 0
        assert d["rules_checked"] == 10
        assert len(d["violations"]) == 1

    def test_summary(self):
        """Test summary generation."""
        results = DRCResults(
            violations=[
                DRCViolation(rule_id="t1", severity="error", message="e1"),
                DRCViolation(rule_id="t2", severity="warning", message="w1"),
            ],
            rules_checked=5,
        )
        summary = results.summary()

        assert "FAILED" in summary
        assert "1 errors" in summary
        assert "1 warnings" in summary
        assert "5 rules checked" in summary

    def test_summary_passed(self):
        """Test summary generation when passed."""
        results = DRCResults(rules_checked=10)
        summary = results.summary()

        assert "PASSED" in summary
        assert "0 errors" in summary


class TestDRCChecker:
    """Tests for the DRCChecker class."""

    def test_checker_instantiation(self, fixtures_dir: Path):
        """Test creating a DRCChecker with valid PCB and manufacturer."""
        # Skip if fixture doesn't exist (we test module structure only)
        from kicad_tools.schema.pcb import PCB

        pcb_file = fixtures_dir / "simple_board.kicad_pcb"
        if not pcb_file.exists():
            pytest.skip("Test PCB fixture not available")

        pcb = PCB.load(pcb_file)
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=4)

        assert checker.manufacturer == "jlcpcb"
        assert checker.layers == 4
        assert checker.design_rules is not None

    def test_checker_unknown_manufacturer(self, fixtures_dir: Path):
        """Test that unknown manufacturer raises error."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = fixtures_dir / "simple_board.kicad_pcb"
        if not pcb_file.exists():
            pytest.skip("Test PCB fixture not available")

        pcb = PCB.load(pcb_file)
        with pytest.raises(ValueError, match="Unknown manufacturer"):
            DRCChecker(pcb, manufacturer="nonexistent_mfr")

    def test_stub_methods_return_empty_results(self, fixtures_dir: Path):
        """Test that stub methods return empty DRCResults."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = fixtures_dir / "simple_board.kicad_pcb"
        if not pcb_file.exists():
            pytest.skip("Test PCB fixture not available")

        pcb = PCB.load(pcb_file)
        checker = DRCChecker(pcb, manufacturer="jlcpcb")

        # Stub methods (not yet implemented) should return empty results
        assert len(checker.check_clearances()) == 0
        assert len(checker.check_edge_clearances()) == 0
        assert len(checker.check_silkscreen()) == 0

        # check_dimensions is implemented (issue #94), check_all may have violations
        # We just verify they return DRCResults instances
        assert isinstance(checker.check_dimensions(), DRCResults)
        assert isinstance(checker.check_all(), DRCResults)

    def test_check_all_aggregates_results(self, fixtures_dir: Path):
        """Test that check_all aggregates results from all checks."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = fixtures_dir / "simple_board.kicad_pcb"
        if not pcb_file.exists():
            pytest.skip("Test PCB fixture not available")

        pcb = PCB.load(pcb_file)
        checker = DRCChecker(pcb, manufacturer="jlcpcb")
        results = checker.check_all()

        # Should be a DRCResults instance
        assert isinstance(results, DRCResults)


class TestModuleImports:
    """Test that public API imports work correctly."""

    def test_import_from_validate(self):
        """Test importing from kicad_tools.validate."""
        from kicad_tools.validate import DRCChecker, DRCResults, DRCViolation

        assert DRCChecker is not None
        assert DRCResults is not None
        assert DRCViolation is not None

    def test_import_checker_directly(self):
        """Test importing DRCChecker directly."""
        from kicad_tools.validate.checker import DRCChecker

        assert DRCChecker is not None

    def test_import_violations_directly(self):
        """Test importing violation classes directly."""
        from kicad_tools.validate.violations import DRCResults, DRCViolation

        assert DRCResults is not None
        assert DRCViolation is not None

    def test_import_base_rule(self):
        """Test importing base rule class."""
        from kicad_tools.validate.rules import DRCRule

        assert DRCRule is not None

    def test_import_dimension_rules(self):
        """Test importing dimension rules class."""
        from kicad_tools.validate.rules import DimensionRules

        assert DimensionRules is not None

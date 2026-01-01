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
        assert len(checker.check_edge_clearances()) == 0
        assert len(checker.check_silkscreen()) == 0

        # check_dimensions and check_clearances are implemented
        # We just verify they return DRCResults instances
        assert isinstance(checker.check_dimensions(), DRCResults)
        assert isinstance(checker.check_clearances(), DRCResults)
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

    def test_import_clearance_rule(self):
        """Test importing ClearanceRule class."""
        from kicad_tools.validate.rules import ClearanceRule

        assert ClearanceRule is not None

    def test_import_edge_clearance_rule(self):
        """Test importing EdgeClearanceRule."""
        from kicad_tools.validate.rules import EdgeClearanceRule

        assert EdgeClearanceRule is not None


# PCB with clearance violations for testing
CLEARANCE_VIOLATION_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "NET1")
  (net 2 "NET2")
  (net 3 "NET3")
  (segment (start 100 100) (end 110 100) (width 0.2) (layer "F.Cu") (net 1) (uuid "seg1"))
  (segment (start 100 100.05) (end 110 100.05) (width 0.2) (layer "F.Cu") (net 2) (uuid "seg2"))
  (via (at 120 100) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1) (uuid "via1"))
  (via (at 120.3 100) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 2) (uuid "via2"))
)
"""

# PCB with no violations (adequate clearances)
CLEARANCE_PASS_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "NET1")
  (net 2 "NET2")
  (segment (start 100 100) (end 110 100) (width 0.2) (layer "F.Cu") (net 1) (uuid "seg1"))
  (segment (start 100 101) (end 110 101) (width 0.2) (layer "F.Cu") (net 2) (uuid "seg2"))
  (via (at 120 100) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1) (uuid "via1"))
  (via (at 122 100) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 2) (uuid "via2"))
)
"""

# PCB with same-net elements (should not trigger violations)
SAME_NET_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "NET1")
  (segment (start 100 100) (end 110 100) (width 0.2) (layer "F.Cu") (net 1) (uuid "seg1"))
  (segment (start 100 100.05) (end 110 100.05) (width 0.2) (layer "F.Cu") (net 1) (uuid "seg2"))
)
"""

# PCB with elements on different layers (should not trigger violations)
DIFFERENT_LAYER_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "NET1")
  (net 2 "NET2")
  (segment (start 100 100) (end 110 100) (width 0.2) (layer "F.Cu") (net 1) (uuid "seg1"))
  (segment (start 100 100) (end 110 100) (width 0.2) (layer "B.Cu") (net 2) (uuid "seg2"))
)
"""

# PCB with edge cuts for testing edge clearance
EDGE_CLEARANCE_TEST_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 0) (end 50 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 50) (end 0 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 0 50) (end 0 0) (layer "Edge.Cuts") (width 0.1))
  (segment (start 0.1 25) (end 10 25) (width 0.2) (layer "F.Cu") (net 1) (uuid "seg-close"))
  (segment (start 25 25) (end 35 25) (width 0.2) (layer "F.Cu") (net 1) (uuid "seg-ok"))
  (via (at 0.2 30) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-close"))
  (via (at 25 30) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-ok"))
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "fp-close")
    (at 1 40)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref1"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "+3.3V"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "fp-ok")
    (at 25 40)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref2"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "+3.3V"))
  )
)
"""


class TestClearanceRule:
    """Tests for the ClearanceRule implementation."""

    def test_trace_to_trace_violation(self, tmp_path: Path):
        """Test detection of trace-to-trace clearance violation."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "clearance_violation.kicad_pcb"
        pcb_file.write_text(CLEARANCE_VIOLATION_PCB)

        pcb = PCB.load(str(pcb_file))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_clearances()

        # Should find violations
        assert len(results) > 0
        assert results.passed is False

        # Check that trace-to-trace violation is reported
        segment_violations = [v for v in results if "segment" in v.rule_id]
        assert len(segment_violations) > 0

    def test_via_to_via_violation(self, tmp_path: Path):
        """Test detection of via-to-via clearance violation."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "clearance_violation.kicad_pcb"
        pcb_file.write_text(CLEARANCE_VIOLATION_PCB)

        pcb = PCB.load(str(pcb_file))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_clearances()

        # Should find via violations
        via_violations = [v for v in results if "via" in v.rule_id]
        assert len(via_violations) > 0

    def test_no_violations_with_adequate_clearance(self, tmp_path: Path):
        """Test that adequate clearances pass."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "clearance_pass.kicad_pcb"
        pcb_file.write_text(CLEARANCE_PASS_PCB)

        pcb = PCB.load(str(pcb_file))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_clearances()

        # Should pass
        assert results.passed is True
        assert len(results) == 0

    def test_same_net_no_violation(self, tmp_path: Path):
        """Test that same-net elements don't trigger violations."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "same_net.kicad_pcb"
        pcb_file.write_text(SAME_NET_PCB)

        pcb = PCB.load(str(pcb_file))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_clearances()

        # Same net elements can touch, no violations expected
        assert results.passed is True
        assert len(results) == 0

    def test_different_layers_no_violation(self, tmp_path: Path):
        """Test that elements on different layers don't trigger violations."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "different_layer.kicad_pcb"
        pcb_file.write_text(DIFFERENT_LAYER_PCB)

        pcb = PCB.load(str(pcb_file))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_clearances()

        # Different layer elements should not conflict
        assert results.passed is True
        assert len(results) == 0

    def test_violation_has_location(self, tmp_path: Path):
        """Test that violations include location information."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "clearance_violation.kicad_pcb"
        pcb_file.write_text(CLEARANCE_VIOLATION_PCB)

        pcb = PCB.load(str(pcb_file))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_clearances()

        # Violations should have location
        for violation in results:
            assert violation.location is not None
            assert len(violation.location) == 2

    def test_violation_has_values(self, tmp_path: Path):
        """Test that violations include actual and required values."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "clearance_violation.kicad_pcb"
        pcb_file.write_text(CLEARANCE_VIOLATION_PCB)

        pcb = PCB.load(str(pcb_file))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_clearances()

        # Violations should have actual/required values
        for violation in results:
            assert violation.actual_value is not None
            assert violation.required_value is not None
            assert violation.actual_value < violation.required_value

    def test_violation_has_layer(self, tmp_path: Path):
        """Test that violations include layer information."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "clearance_violation.kicad_pcb"
        pcb_file.write_text(CLEARANCE_VIOLATION_PCB)

        pcb = PCB.load(str(pcb_file))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_clearances()

        # Violations should have layer
        for violation in results:
            assert violation.layer is not None
            assert violation.layer in ("F.Cu", "B.Cu")

    def test_violation_has_items(self, tmp_path: Path):
        """Test that violations include item references."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "clearance_violation.kicad_pcb"
        pcb_file.write_text(CLEARANCE_VIOLATION_PCB)

        pcb = PCB.load(str(pcb_file))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_clearances()

        # Violations should have item references
        for violation in results:
            assert len(violation.items) == 2

    def test_rules_checked_count(self, tmp_path: Path):
        """Test that rules_checked reflects number of copper layers checked."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "clearance_pass.kicad_pcb"
        pcb_file.write_text(CLEARANCE_PASS_PCB)

        pcb = PCB.load(str(pcb_file))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_clearances()

        # Should check both F.Cu and B.Cu
        assert results.rules_checked == 2


class TestClearanceRuleDistanceCalculations:
    """Unit tests for distance calculation functions."""

    def test_point_to_segment_distance(self):
        """Test point-to-segment distance calculation."""
        from kicad_tools.validate.rules.clearance import _point_to_segment_distance

        # Point directly above segment midpoint
        dist = _point_to_segment_distance(5, 1, 0, 0, 10, 0)
        assert dist == pytest.approx(1.0)

        # Point at segment start
        dist = _point_to_segment_distance(0, 0, 0, 0, 10, 0)
        assert dist == pytest.approx(0.0)

        # Point to the left of segment
        dist = _point_to_segment_distance(-1, 0, 0, 0, 10, 0)
        assert dist == pytest.approx(1.0)

    def test_segment_to_segment_distance(self):
        """Test segment-to-segment distance calculation."""
        from kicad_tools.validate.rules.clearance import _segment_to_segment_distance

        # Parallel segments
        dist = _segment_to_segment_distance(0, 0, 10, 0, 0, 1, 10, 1)
        assert dist == pytest.approx(1.0)

        # Touching segments
        dist = _segment_to_segment_distance(0, 0, 10, 0, 10, 0, 20, 0)
        assert dist == pytest.approx(0.0)

    def test_circle_circle_clearance(self):
        """Test circle-to-circle clearance calculation."""
        from kicad_tools.validate.rules.clearance import (
            CopperElement,
            _circle_circle_clearance,
        )

        # Two circles with known geometry
        c1 = CopperElement(
            element_type="via",
            layer="F.Cu",
            net_number=1,
            geometry=(0, 0, 1.0, 1.0),  # center at origin, diameter 1
            reference="Via1",
        )
        c2 = CopperElement(
            element_type="via",
            layer="F.Cu",
            net_number=2,
            geometry=(2, 0, 1.0, 1.0),  # center at (2,0), diameter 1
            reference="Via2",
        )

        clearance, loc_x, loc_y = _circle_circle_clearance(c1, c2)
        # Distance between centers = 2, radii = 0.5 each
        # Clearance = 2 - 0.5 - 0.5 = 1.0
        assert clearance == pytest.approx(1.0)
        assert loc_x == pytest.approx(1.0)  # midpoint
        assert loc_y == pytest.approx(0.0)

    def test_copper_element_from_segment(self):
        """Test creating CopperElement from Segment."""
        from kicad_tools.schema.pcb import Segment
        from kicad_tools.validate.rules.clearance import CopperElement

        seg = Segment(
            start=(0.0, 0.0),
            end=(10.0, 0.0),
            width=0.2,
            layer="F.Cu",
            net_number=1,
            uuid="test-uuid",
        )

        elem = CopperElement.from_segment(seg)
        assert elem.element_type == "segment"
        assert elem.layer == "F.Cu"
        assert elem.net_number == 1
        assert elem.geometry == (0.0, 0.0, 10.0, 0.0, 0.2)

    def test_copper_element_from_via(self):
        """Test creating CopperElement from Via."""
        from kicad_tools.schema.pcb import Via
        from kicad_tools.validate.rules.clearance import CopperElement

        via = Via(
            position=(100.0, 100.0),
            size=0.8,
            drill=0.4,
            layers=["F.Cu", "B.Cu"],
            net_number=1,
            uuid="test-uuid",
        )

        elem = CopperElement.from_via(via)
        assert elem.element_type == "via"
        assert elem.layer == "*"  # Vias span layers
        assert elem.net_number == 1
        assert elem.geometry == (100.0, 100.0, 0.8, 0.8)


class TestGraphicLineParsing:
    """Tests for GraphicLine parsing."""

    def test_parse_graphic_lines(self, tmp_path: Path):
        """Test that graphic lines are parsed from PCB."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(EDGE_CLEARANCE_TEST_PCB)

        pcb = PCB.load(str(pcb_file))

        assert len(pcb.graphic_lines) == 4
        assert all(line.layer == "Edge.Cuts" for line in pcb.graphic_lines)

    def test_graphic_line_properties(self, tmp_path: Path):
        """Test graphic line properties are parsed correctly."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(EDGE_CLEARANCE_TEST_PCB)

        pcb = PCB.load(str(pcb_file))
        line = pcb.graphic_lines[0]

        assert line.start == (0.0, 0.0)
        assert line.end == (50.0, 0.0)
        assert line.layer == "Edge.Cuts"
        assert line.width == pytest.approx(0.1)


class TestBoardOutline:
    """Tests for board outline extraction."""

    def test_get_board_outline(self, tmp_path: Path):
        """Test extracting board outline from Edge.Cuts."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(EDGE_CLEARANCE_TEST_PCB)

        pcb = PCB.load(str(pcb_file))
        outline = pcb.get_board_outline()

        # Should have 5 points (closed rectangle: 4 corners + return to start)
        assert len(outline) >= 4

    def test_get_board_outline_segments(self, tmp_path: Path):
        """Test getting board outline as segments."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(EDGE_CLEARANCE_TEST_PCB)

        pcb = PCB.load(str(pcb_file))
        segments = pcb.get_board_outline_segments()

        # Should have 4 segments for the rectangle
        assert len(segments) == 4
        for seg_start, seg_end in segments:
            assert isinstance(seg_start, tuple)
            assert isinstance(seg_end, tuple)
            assert len(seg_start) == 2
            assert len(seg_end) == 2

    def test_empty_board_outline(self, tmp_path: Path):
        """Test empty outline when no Edge.Cuts graphics."""
        from kicad_tools.schema.pcb import PCB

        # PCB without Edge.Cuts
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (layers (0 "F.Cu" signal) (44 "Edge.Cuts" user))
          (net 0 "")
        )"""
        pcb_file = tmp_path / "no_edge.kicad_pcb"
        pcb_file.write_text(pcb_content)

        pcb = PCB.load(str(pcb_file))
        outline = pcb.get_board_outline()

        assert outline == []


class TestEdgeClearanceRule:
    """Tests for EdgeClearanceRule."""

    def test_edge_clearance_rule_properties(self):
        """Test EdgeClearanceRule class properties."""
        from kicad_tools.validate.rules.edge import EdgeClearanceRule

        rule = EdgeClearanceRule()
        assert rule.rule_id == "edge_clearance"
        assert rule.name == "Edge Clearance"

    def test_trace_too_close_to_edge(self, tmp_path: Path):
        """Test that traces too close to edge are detected."""
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(EDGE_CLEARANCE_TEST_PCB)

        pcb = PCB.load(str(pcb_file))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_edge_clearances()

        # Should find violations for elements too close to edge
        trace_violations = [v for v in results if "edge_clearance_trace" in v.rule_id]
        assert len(trace_violations) > 0

    def test_via_too_close_to_edge(self, tmp_path: Path):
        """Test that vias too close to edge are detected."""
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(EDGE_CLEARANCE_TEST_PCB)

        pcb = PCB.load(str(pcb_file))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_edge_clearances()

        # Should find violations for via too close to edge
        via_violations = [v for v in results if "edge_clearance_via" in v.rule_id]
        assert len(via_violations) > 0

    def test_pad_too_close_to_edge(self, tmp_path: Path):
        """Test that pads too close to edge are detected."""
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(EDGE_CLEARANCE_TEST_PCB)

        pcb = PCB.load(str(pcb_file))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_edge_clearances()

        # Should find violations for pad too close to edge
        pad_violations = [v for v in results if "edge_clearance_pad" in v.rule_id]
        assert len(pad_violations) > 0

    def test_no_violations_for_centered_elements(self, tmp_path: Path):
        """Test that centered elements don't trigger violations."""
        # PCB with elements well within clearance limits
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (layers (0 "F.Cu" signal) (44 "Edge.Cuts" user))
          (net 0 "")
          (net 1 "GND")
          (gr_line (start 0 0) (end 100 0) (layer "Edge.Cuts") (width 0.1))
          (gr_line (start 100 0) (end 100 100) (layer "Edge.Cuts") (width 0.1))
          (gr_line (start 100 100) (end 0 100) (layer "Edge.Cuts") (width 0.1))
          (gr_line (start 0 100) (end 0 0) (layer "Edge.Cuts") (width 0.1))
          (segment (start 50 50) (end 60 50) (width 0.2) (layer "F.Cu") (net 1))
          (via (at 55 55) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1))
        )"""

        pcb_file = tmp_path / "centered.kicad_pcb"
        pcb_file.write_text(pcb_content)

        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb = PCB.load(str(pcb_file))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_edge_clearances()

        # Elements at center should not trigger violations
        assert len(results.errors) == 0

    def test_no_violations_without_edge_cuts(self, tmp_path: Path):
        """Test that no violations are reported when there's no board outline."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (layers (0 "F.Cu" signal) (44 "Edge.Cuts" user))
          (net 0 "")
          (net 1 "GND")
          (segment (start 0 0) (end 10 0) (width 0.2) (layer "F.Cu") (net 1))
        )"""

        pcb_file = tmp_path / "no_outline.kicad_pcb"
        pcb_file.write_text(pcb_content)

        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb = PCB.load(str(pcb_file))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_edge_clearances()

        # No outline = no edge clearance checks
        assert len(results) == 0

    def test_violation_includes_location_and_values(self, tmp_path: Path):
        """Test that violations include location and clearance values."""
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(EDGE_CLEARANCE_TEST_PCB)

        pcb = PCB.load(str(pcb_file))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_edge_clearances()

        # Get first violation
        if results.errors:
            v = results.errors[0]
            assert v.location is not None
            assert v.actual_value is not None
            assert v.required_value is not None
            assert v.actual_value < v.required_value


class TestPointToSegmentDistance:
    """Tests for point-to-segment distance calculation."""

    def test_point_on_segment(self):
        """Test distance is 0 for point on segment."""
        from kicad_tools.validate.rules.edge import EdgeClearanceRule

        rule = EdgeClearanceRule()
        dist = rule._point_to_segment_distance((5, 0), (0, 0), (10, 0))
        assert dist == pytest.approx(0.0)

    def test_point_perpendicular_to_segment(self):
        """Test distance for point perpendicular to segment."""
        from kicad_tools.validate.rules.edge import EdgeClearanceRule

        rule = EdgeClearanceRule()
        dist = rule._point_to_segment_distance((5, 3), (0, 0), (10, 0))
        assert dist == pytest.approx(3.0)

    def test_point_closest_to_start(self):
        """Test distance when closest point is segment start."""
        from kicad_tools.validate.rules.edge import EdgeClearanceRule

        rule = EdgeClearanceRule()
        dist = rule._point_to_segment_distance((-3, 4), (0, 0), (10, 0))
        assert dist == pytest.approx(5.0)  # 3-4-5 triangle

    def test_point_closest_to_end(self):
        """Test distance when closest point is segment end."""
        from kicad_tools.validate.rules.edge import EdgeClearanceRule

        rule = EdgeClearanceRule()
        dist = rule._point_to_segment_distance((13, 4), (0, 0), (10, 0))
        assert dist == pytest.approx(5.0)  # 3-4-5 triangle

    def test_zero_length_segment(self):
        """Test distance to zero-length segment (point)."""
        from kicad_tools.validate.rules.edge import EdgeClearanceRule

        rule = EdgeClearanceRule()
        dist = rule._point_to_segment_distance((3, 4), (0, 0), (0, 0))
        assert dist == pytest.approx(5.0)  # 3-4-5 triangle

    def test_import_silkscreen_rules(self):
        """Test importing silkscreen rule functions."""
        from kicad_tools.validate.rules import (
            check_all_silkscreen,
            check_silkscreen_line_width,
            check_silkscreen_over_pads,
            check_silkscreen_text_height,
        )

        assert check_all_silkscreen is not None
        assert check_silkscreen_line_width is not None
        assert check_silkscreen_over_pads is not None
        assert check_silkscreen_text_height is not None


class TestSilkscreenRules:
    """Tests for silkscreen validation rules."""

    def test_silkscreen_text_height_violation(self):
        """Test detection of silkscreen text below minimum height."""
        from kicad_tools.manufacturers import get_profile
        from kicad_tools.schema.pcb import PCB, Footprint, FootprintText
        from kicad_tools.sexp import SExp
        from kicad_tools.validate.rules.silkscreen import check_silkscreen_text_height

        # Create a minimal PCB with a footprint that has small text
        sexp = SExp(name="kicad_pcb")
        pcb = PCB(sexp)

        # Manually add a footprint with text smaller than minimum
        fp = Footprint(
            name="TestFP",
            layer="F.Cu",
            position=(10.0, 20.0),
            rotation=0.0,
            reference="U1",
            value="TEST",
            pads=[],
            texts=[
                FootprintText(
                    text_type="reference",
                    text="U1",
                    position=(0.0, -2.0),
                    layer="F.SilkS",  # Silkscreen layer
                    font_size=(0.5, 0.5),  # Below minimum of 0.8mm
                    font_thickness=0.1,
                ),
            ],
            graphics=[],
        )
        pcb._footprints.append(fp)

        # Get design rules
        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=4)

        # Check silkscreen text height
        results = check_silkscreen_text_height(pcb, rules)

        # Should have one violation
        assert len(results) == 1
        assert results.violations[0].rule_id == "silkscreen_text_height"
        assert results.violations[0].severity == "warning"
        assert results.violations[0].actual_value == pytest.approx(0.5)
        assert results.violations[0].required_value == pytest.approx(0.8)

    def test_silkscreen_text_height_no_violation(self):
        """Test no violation for silkscreen text at/above minimum height."""
        from kicad_tools.manufacturers import get_profile
        from kicad_tools.schema.pcb import PCB, Footprint, FootprintText
        from kicad_tools.sexp import SExp
        from kicad_tools.validate.rules.silkscreen import check_silkscreen_text_height

        # Create PCB with footprint that has adequate text height
        sexp = SExp(name="kicad_pcb")
        pcb = PCB(sexp)

        fp = Footprint(
            name="TestFP",
            layer="F.Cu",
            position=(10.0, 20.0),
            rotation=0.0,
            reference="U1",
            value="TEST",
            pads=[],
            texts=[
                FootprintText(
                    text_type="reference",
                    text="U1",
                    position=(0.0, -2.0),
                    layer="F.SilkS",
                    font_size=(1.0, 1.0),  # Above minimum of 0.8mm
                    font_thickness=0.15,
                ),
            ],
            graphics=[],
        )
        pcb._footprints.append(fp)

        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=4)

        results = check_silkscreen_text_height(pcb, rules)

        # Should have no violations
        assert len(results) == 0
        assert results.passed is True

    def test_silkscreen_line_width_violation(self):
        """Test detection of silkscreen line below minimum width."""
        from kicad_tools.manufacturers import get_profile
        from kicad_tools.schema.pcb import PCB, Footprint, FootprintGraphic
        from kicad_tools.sexp import SExp
        from kicad_tools.validate.rules.silkscreen import check_silkscreen_line_width

        sexp = SExp(name="kicad_pcb")
        pcb = PCB(sexp)

        fp = Footprint(
            name="TestFP",
            layer="F.Cu",
            position=(10.0, 20.0),
            rotation=0.0,
            reference="U1",
            value="TEST",
            pads=[],
            texts=[],
            graphics=[
                FootprintGraphic(
                    graphic_type="line",
                    layer="F.SilkS",
                    stroke_width=0.10,  # Below minimum of 0.15mm
                    start=(0.0, 0.0),
                    end=(5.0, 0.0),
                ),
            ],
        )
        pcb._footprints.append(fp)

        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=4)

        results = check_silkscreen_line_width(pcb, rules)

        assert len(results) == 1
        assert results.violations[0].rule_id == "silkscreen_line_width"
        assert results.violations[0].actual_value == pytest.approx(0.10)
        assert results.violations[0].required_value == pytest.approx(0.15)

    def test_silkscreen_line_width_no_violation(self):
        """Test no violation for silkscreen line at/above minimum width."""
        from kicad_tools.manufacturers import get_profile
        from kicad_tools.schema.pcb import PCB, Footprint, FootprintGraphic
        from kicad_tools.sexp import SExp
        from kicad_tools.validate.rules.silkscreen import check_silkscreen_line_width

        sexp = SExp(name="kicad_pcb")
        pcb = PCB(sexp)

        fp = Footprint(
            name="TestFP",
            layer="F.Cu",
            position=(10.0, 20.0),
            rotation=0.0,
            reference="U1",
            value="TEST",
            pads=[],
            texts=[],
            graphics=[
                FootprintGraphic(
                    graphic_type="line",
                    layer="F.SilkS",
                    stroke_width=0.15,  # At minimum of 0.15mm
                    start=(0.0, 0.0),
                    end=(5.0, 0.0),
                ),
            ],
        )
        pcb._footprints.append(fp)

        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=4)

        results = check_silkscreen_line_width(pcb, rules)

        assert len(results) == 0
        assert results.passed is True

    def test_silkscreen_hidden_text_ignored(self):
        """Test that hidden silkscreen text is not checked."""
        from kicad_tools.manufacturers import get_profile
        from kicad_tools.schema.pcb import PCB, Footprint, FootprintText
        from kicad_tools.sexp import SExp
        from kicad_tools.validate.rules.silkscreen import check_silkscreen_text_height

        sexp = SExp(name="kicad_pcb")
        pcb = PCB(sexp)

        fp = Footprint(
            name="TestFP",
            layer="F.Cu",
            position=(10.0, 20.0),
            rotation=0.0,
            reference="U1",
            value="TEST",
            pads=[],
            texts=[
                FootprintText(
                    text_type="reference",
                    text="U1",
                    position=(0.0, -2.0),
                    layer="F.SilkS",
                    font_size=(0.5, 0.5),  # Below minimum but hidden
                    font_thickness=0.1,
                    hidden=True,
                ),
            ],
            graphics=[],
        )
        pcb._footprints.append(fp)

        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=4)

        results = check_silkscreen_text_height(pcb, rules)

        # Hidden text should be ignored
        assert len(results) == 0

    def test_silkscreen_non_silkscreen_layer_ignored(self):
        """Test that text on non-silkscreen layers is not checked."""
        from kicad_tools.manufacturers import get_profile
        from kicad_tools.schema.pcb import PCB, Footprint, FootprintText
        from kicad_tools.sexp import SExp
        from kicad_tools.validate.rules.silkscreen import check_silkscreen_text_height

        sexp = SExp(name="kicad_pcb")
        pcb = PCB(sexp)

        fp = Footprint(
            name="TestFP",
            layer="F.Cu",
            position=(10.0, 20.0),
            rotation=0.0,
            reference="U1",
            value="TEST",
            pads=[],
            texts=[
                FootprintText(
                    text_type="value",
                    text="TEST",
                    position=(0.0, 2.0),
                    layer="F.Fab",  # Not a silkscreen layer
                    font_size=(0.5, 0.5),  # Small but on Fab layer
                    font_thickness=0.1,
                ),
            ],
            graphics=[],
        )
        pcb._footprints.append(fp)

        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=4)

        results = check_silkscreen_text_height(pcb, rules)

        # Text on F.Fab should be ignored
        assert len(results) == 0

    def test_check_all_silkscreen(self):
        """Test that check_all_silkscreen combines all checks."""
        from kicad_tools.manufacturers import get_profile
        from kicad_tools.schema.pcb import (
            PCB,
            Footprint,
            FootprintGraphic,
            FootprintText,
        )
        from kicad_tools.sexp import SExp
        from kicad_tools.validate.rules.silkscreen import check_all_silkscreen

        sexp = SExp(name="kicad_pcb")
        pcb = PCB(sexp)

        # Add footprint with both text and line violations
        fp = Footprint(
            name="TestFP",
            layer="F.Cu",
            position=(10.0, 20.0),
            rotation=0.0,
            reference="U1",
            value="TEST",
            pads=[],
            texts=[
                FootprintText(
                    text_type="reference",
                    text="U1",
                    position=(0.0, -2.0),
                    layer="F.SilkS",
                    font_size=(0.5, 0.5),  # Violation
                    font_thickness=0.1,
                ),
            ],
            graphics=[
                FootprintGraphic(
                    graphic_type="line",
                    layer="F.SilkS",
                    stroke_width=0.10,  # Violation
                    start=(0.0, 0.0),
                    end=(5.0, 0.0),
                ),
            ],
        )
        pcb._footprints.append(fp)

        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=4)

        results = check_all_silkscreen(pcb, rules)

        # Should have 2 violations (text + line)
        assert len(results) == 2
        assert results.rules_checked == 3  # 3 rule types checked

        # Check that both rule types are represented
        rule_ids = {v.rule_id for v in results.violations}
        assert "silkscreen_text_height" in rule_ids
        assert "silkscreen_line_width" in rule_ids

    def test_drc_checker_silkscreen_integration(self):
        """Test DRCChecker.check_silkscreen integration."""
        from kicad_tools.schema.pcb import (
            PCB,
            Footprint,
            FootprintText,
        )
        from kicad_tools.sexp import SExp
        from kicad_tools.validate import DRCChecker

        sexp = SExp(name="kicad_pcb")
        pcb = PCB(sexp)

        fp = Footprint(
            name="TestFP",
            layer="F.Cu",
            position=(10.0, 20.0),
            rotation=0.0,
            reference="U1",
            value="TEST",
            pads=[],
            texts=[
                FootprintText(
                    text_type="reference",
                    text="U1",
                    position=(0.0, -2.0),
                    layer="F.SilkS",
                    font_size=(0.5, 0.5),  # Violation
                    font_thickness=0.1,
                ),
            ],
            graphics=[],
        )
        pcb._footprints.append(fp)

        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=4)
        results = checker.check_silkscreen()

        assert len(results) >= 1
        assert any(v.rule_id == "silkscreen_text_height" for v in results.violations)

    def test_silkscreen_back_layer(self):
        """Test silkscreen checks on back silkscreen layer."""
        from kicad_tools.manufacturers import get_profile
        from kicad_tools.schema.pcb import PCB, Footprint, FootprintText
        from kicad_tools.sexp import SExp
        from kicad_tools.validate.rules.silkscreen import check_silkscreen_text_height

        sexp = SExp(name="kicad_pcb")
        pcb = PCB(sexp)

        fp = Footprint(
            name="TestFP",
            layer="B.Cu",
            position=(10.0, 20.0),
            rotation=0.0,
            reference="U1",
            value="TEST",
            pads=[],
            texts=[
                FootprintText(
                    text_type="reference",
                    text="U1",
                    position=(0.0, -2.0),
                    layer="B.SilkS",  # Back silkscreen
                    font_size=(0.5, 0.5),  # Below minimum
                    font_thickness=0.1,
                ),
            ],
            graphics=[],
        )
        pcb._footprints.append(fp)

        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=4)

        results = check_silkscreen_text_height(pcb, rules)

        assert len(results) == 1
        assert results.violations[0].layer == "B.SilkS"

    def test_real_pcb_silkscreen(self):
        """Test silkscreen checks on a real PCB file with fp_text elements."""
        from pathlib import Path

        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        # Use the example PCB that has fp_text elements
        pcb_file = (
            Path(__file__).parent.parent
            / "examples"
            / "04-autorouter"
            / "usb_joystick"
            / "usb_joystick.kicad_pcb"
        )

        if not pcb_file.exists():
            pytest.skip("Example PCB file not available")

        pcb = PCB.load(str(pcb_file))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)

        # Run silkscreen checks
        results = checker.check_silkscreen()

        # The PCB has text with 0.5mm height (C1-C4) which is below 0.8mm minimum
        # Should detect violations
        text_violations = [v for v in results.violations if v.rule_id == "silkscreen_text_height"]
        assert len(text_violations) > 0

        # Check that rules_checked is correct (3 silkscreen rules)
        assert results.rules_checked == 3

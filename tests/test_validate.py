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

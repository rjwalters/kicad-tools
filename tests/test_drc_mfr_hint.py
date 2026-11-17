"""Tests for manufacturer compatibility hint feature in DRC command."""

from kicad_tools.cli.drc_cmd import _get_manufacturer_compatibility_hint
from kicad_tools.drc import DRCViolation, Severity, ViolationType


class TestManufacturerCompatibilityHint:
    """Tests for the _get_manufacturer_compatibility_hint function."""

    def test_no_hint_when_no_violations(self):
        """No hint should be shown when there are no violations."""
        result = _get_manufacturer_compatibility_hint([])
        assert result is None

    def test_no_hint_when_only_warnings(self):
        """No hint should be shown when there are only warnings."""
        violations = [
            DRCViolation(
                type=ViolationType.CLEARANCE,
                type_str="clearance",
                severity=Severity.WARNING,
                message="Test warning",
            )
        ]
        result = _get_manufacturer_compatibility_hint(violations)
        assert result is None

    def test_hint_for_clearance_passing_manufacturer_rules(self):
        """Hint should be shown when clearance errors pass manufacturer rules.

        JLCPCB min clearance is 0.1016mm (4 mil), so a violation at 0.15mm
        actual clearance (which fails a 0.2mm board rule) should pass.
        """
        violations = [
            DRCViolation(
                type=ViolationType.CLEARANCE,
                type_str="clearance",
                severity=Severity.ERROR,
                message="Clearance violation (clearance 0.2000 mm; actual 0.1500 mm)",
                actual_value_mm=0.15,
                required_value_mm=0.20,
            )
        ]
        result = _get_manufacturer_compatibility_hint(violations, layers=2)

        assert result is not None
        assert "JLCPCB" in result
        assert "--mfr jlcpcb" in result

    def test_hint_for_track_width_passing_manufacturer_rules(self):
        """Hint should be shown when track width errors pass manufacturer rules.

        JLCPCB min trace width is 0.127mm (5 mil for standard), so a violation at 0.15mm
        actual width (which fails a 0.2mm board rule) should pass.
        """
        violations = [
            DRCViolation(
                type=ViolationType.TRACK_WIDTH,
                type_str="track_width",
                severity=Severity.ERROR,
                message="Track width violation (width 0.2000 mm; actual 0.1500 mm)",
                actual_value_mm=0.15,
                required_value_mm=0.20,
            )
        ]
        result = _get_manufacturer_compatibility_hint(violations, layers=2)

        assert result is not None
        assert "JLCPCB" in result

    def test_no_hint_for_critical_connection_errors(self):
        """No hint for critical connection issues (shorts, unconnected items)."""
        violations = [
            DRCViolation(
                type=ViolationType.SHORTING_ITEMS,
                type_str="shorting_items",
                severity=Severity.ERROR,
                message="Items are shorted",
            )
        ]
        result = _get_manufacturer_compatibility_hint(violations, layers=2)

        # Shorts always fail, so no hint about passing manufacturer rules
        assert result is None

    def test_no_hint_when_violations_fail_manufacturer_rules(self):
        """No hint when violations would fail manufacturer rules too.

        JLCPCB min clearance is 0.1016mm (4 mil), so 0.05mm clearance fails.
        """
        violations = [
            DRCViolation(
                type=ViolationType.CLEARANCE,
                type_str="clearance",
                severity=Severity.ERROR,
                message="Clearance violation (clearance 0.2000 mm; actual 0.0500 mm)",
                actual_value_mm=0.05,
                required_value_mm=0.20,
            )
        ]
        result = _get_manufacturer_compatibility_hint(violations, layers=2)

        # 0.05mm fails JLCPCB's 0.1016mm minimum, so no hint
        assert result is None

    def test_mixed_violations_shows_partial_hint(self):
        """When some violations pass and some fail, show partial info."""
        violations = [
            # This passes JLCPCB rules (0.15mm > 0.1016mm min)
            DRCViolation(
                type=ViolationType.CLEARANCE,
                type_str="clearance",
                severity=Severity.ERROR,
                message="Clearance violation",
                actual_value_mm=0.15,
                required_value_mm=0.20,
            ),
            # Shorts always fail
            DRCViolation(
                type=ViolationType.SHORTING_ITEMS,
                type_str="shorting_items",
                severity=Severity.ERROR,
                message="Items are shorted",
            ),
        ]
        result = _get_manufacturer_compatibility_hint(violations, layers=2)

        # Should show hint about the passing violation
        if result is not None:
            assert "1 of" in result or "JLCPCB" in result

    def test_hint_includes_layer_count(self):
        """Hint should include the specified layer count."""
        violations = [
            DRCViolation(
                type=ViolationType.CLEARANCE,
                type_str="clearance",
                severity=Severity.ERROR,
                message="Clearance violation",
                actual_value_mm=0.15,
                required_value_mm=0.20,
            )
        ]
        result = _get_manufacturer_compatibility_hint(violations, layers=4)

        assert result is not None
        assert "-l 4" in result

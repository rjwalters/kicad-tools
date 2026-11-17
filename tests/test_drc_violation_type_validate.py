"""Tests for ViolationType.from_string with validate-module rule_ids.

Ensures that every rule_id produced by the pure-Python validate checker
maps to a specific (non-UNKNOWN) ViolationType enum member, and that the
validate DRCViolation.to_dict() round-trips correctly through
drc.report._parse_kct_check_json().
"""

import pytest

from kicad_tools.drc.violation import ViolationType


class TestValidateRuleIdMapping:
    """ViolationType.from_string must resolve every validate rule_id."""

    # -- clearance subtypes (element_type pairs: pad, segment, via) ----------

    @pytest.mark.parametrize(
        "rule_id, expected",
        [
            ("clearance_pad_pad", ViolationType.CLEARANCE_PAD_PAD),
            ("clearance_pad_segment", ViolationType.CLEARANCE_PAD_SEGMENT),
            ("clearance_pad_via", ViolationType.CLEARANCE_PAD_VIA),
            ("clearance_segment_segment", ViolationType.CLEARANCE_SEGMENT_SEGMENT),
            ("clearance_segment_via", ViolationType.CLEARANCE_SEGMENT_VIA),
            ("clearance_via_via", ViolationType.CLEARANCE_VIA_VIA),
        ],
    )
    def test_clearance_subtypes(self, rule_id: str, expected: ViolationType):
        assert ViolationType.from_string(rule_id) == expected

    # -- "trace" synonyms for "segment" -------------------------------------

    @pytest.mark.parametrize(
        "rule_id, expected",
        [
            ("clearance_pad_trace", ViolationType.CLEARANCE_PAD_SEGMENT),
            ("clearance_trace_trace", ViolationType.CLEARANCE_SEGMENT_SEGMENT),
            ("clearance_trace_via", ViolationType.CLEARANCE_SEGMENT_VIA),
        ],
    )
    def test_clearance_trace_synonyms(self, rule_id: str, expected: ViolationType):
        assert ViolationType.from_string(rule_id) == expected

    # -- edge clearance subtypes ---------------------------------------------

    @pytest.mark.parametrize(
        "rule_id, expected",
        [
            ("edge_clearance_trace", ViolationType.EDGE_CLEARANCE_TRACE),
            ("edge_clearance_pad", ViolationType.EDGE_CLEARANCE_PAD),
            ("edge_clearance_pad_hole", ViolationType.EDGE_CLEARANCE_PAD_HOLE),
            ("edge_clearance_via", ViolationType.EDGE_CLEARANCE_VIA),
            ("edge_clearance_zone", ViolationType.EDGE_CLEARANCE_ZONE),
        ],
    )
    def test_edge_clearance_subtypes(self, rule_id: str, expected: ViolationType):
        assert ViolationType.from_string(rule_id) == expected

    # -- dimension rules -----------------------------------------------------

    @pytest.mark.parametrize(
        "rule_id, expected",
        [
            ("dimension_trace_width", ViolationType.DIMENSION_TRACE_WIDTH),
            ("dimension_via_drill", ViolationType.DIMENSION_VIA_DRILL),
            ("dimension_via_diameter", ViolationType.DIMENSION_VIA_DIAMETER),
            ("dimension_annular_ring", ViolationType.DIMENSION_ANNULAR_RING),
            # Issue #4353: canonical id is now ``hole_to_hole_clearance``; the
            # legacy ``dimension_drill_clearance`` still resolves via the
            # backwards-compat alias (asserted in test_hole_to_hole_compat_alias).
            ("hole_to_hole_clearance", ViolationType.HOLE_TO_HOLE_CLEARANCE),
        ],
    )
    def test_dimension_rules(self, rule_id: str, expected: ViolationType):
        assert ViolationType.from_string(rule_id) == expected

    def test_hole_to_hole_compat_alias(self):
        """Issue #4353: the legacy ``dimension_drill_clearance`` rule_id must
        still resolve to the renamed member so previously-saved JSON reports
        and ``kct fix-drc dimension_drill_clearance`` keep working."""
        assert (
            ViolationType.from_string("dimension_drill_clearance")
            == ViolationType.HOLE_TO_HOLE_CLEARANCE
        )
        # And the new canonical string resolves to the same member.
        assert (
            ViolationType.from_string("hole_to_hole_clearance")
            == ViolationType.HOLE_TO_HOLE_CLEARANCE
        )

    # -- silkscreen rules ----------------------------------------------------

    @pytest.mark.parametrize(
        "rule_id, expected",
        [
            ("silkscreen_line_width", ViolationType.SILKSCREEN_LINE_WIDTH),
            ("silkscreen_text_height", ViolationType.SILKSCREEN_TEXT_HEIGHT),
            ("silkscreen_over_pad", ViolationType.SILKSCREEN_OVER_PAD),
        ],
    )
    def test_silkscreen_rules(self, rule_id: str, expected: ViolationType):
        assert ViolationType.from_string(rule_id) == expected

    # -- solder mask rules ---------------------------------------------------

    @pytest.mark.parametrize(
        "rule_id, expected",
        [
            ("solder_mask_clearance", ViolationType.SOLDER_MASK_CLEARANCE),
            ("min_pad_size", ViolationType.MIN_PAD_SIZE),
            ("pth_annular_ring", ViolationType.PTH_ANNULAR_RING),
        ],
    )
    def test_solder_mask_rules(self, rule_id: str, expected: ViolationType):
        assert ViolationType.from_string(rule_id) == expected

    # -- impedance rule ------------------------------------------------------

    def test_impedance(self):
        assert ViolationType.from_string("impedance") == ViolationType.IMPEDANCE

    # -- no validate rule_id should resolve to UNKNOWN -----------------------

    @pytest.mark.parametrize(
        "rule_id",
        [
            "clearance_pad_pad",
            "clearance_pad_segment",
            "clearance_pad_via",
            "clearance_segment_segment",
            "clearance_segment_via",
            "clearance_via_via",
            "clearance_pad_trace",
            "clearance_trace_trace",
            "clearance_trace_via",
            "edge_clearance_trace",
            "edge_clearance_pad",
            "edge_clearance_pad_hole",
            "edge_clearance_via",
            "edge_clearance_zone",
            "dimension_trace_width",
            "dimension_via_drill",
            "dimension_via_diameter",
            "dimension_annular_ring",
            "hole_to_hole_clearance",
            # Issue #4353: legacy alias must still resolve (never UNKNOWN).
            "dimension_drill_clearance",
            "silkscreen_line_width",
            "silkscreen_text_height",
            "silkscreen_over_pad",
            "solder_mask_clearance",
            "min_pad_size",
            "pth_annular_ring",
            "impedance",
        ],
    )
    def test_no_unknown_for_validate_rules(self, rule_id: str):
        """Every validate rule_id must resolve to a specific type, never UNKNOWN."""
        result = ViolationType.from_string(rule_id)
        assert result != ViolationType.UNKNOWN, f"rule_id {rule_id!r} mapped to UNKNOWN"


class TestValidateViolationToDict:
    """validate.violations.DRCViolation.to_dict() includes a type field."""

    def test_to_dict_has_type_field(self):
        from kicad_tools.validate.violations import DRCViolation

        v = DRCViolation(
            rule_id="dimension_trace_width",
            severity="error",
            message="Trace width 0.10mm < minimum 0.13mm",
            location=(10.0, 20.0),
            layer="F.Cu",
            actual_value=0.10,
            required_value=0.13,
        )
        d = v.to_dict()
        assert "type" in d
        assert d["type"] == "dimension_trace_width"
        assert d["rule_id"] == "dimension_trace_width"

    def test_to_dict_type_not_unknown(self):
        from kicad_tools.validate.violations import DRCViolation

        v = DRCViolation(
            rule_id="silkscreen_line_width",
            severity="warning",
            message="Silkscreen line too thin",
        )
        d = v.to_dict()
        assert d["type"] != "unknown"
        assert d["type"] == "silkscreen_line_width"

    def test_to_dict_clearance_type(self):
        from kicad_tools.validate.violations import DRCViolation

        v = DRCViolation(
            rule_id="clearance_pad_pad",
            severity="error",
            message="Pad clearance too small",
        )
        d = v.to_dict()
        assert d["type"] == "clearance_pad_pad"


class TestKctCheckJsonRoundTrip:
    """Verify kct-check JSON -> _parse_kct_check_json preserves rule types."""

    def test_round_trip_preserves_type(self):
        from kicad_tools.drc.report import _parse_kct_check_json

        kct_json = {
            "file": "/tmp/test.kicad_pcb",
            "manufacturer": "jlcpcb",
            "layers": 2,
            "summary": {"errors": 2, "warnings": 1, "rules_checked": 5, "passed": False},
            "violations": [
                {
                    "rule_id": "dimension_trace_width",
                    "severity": "error",
                    "message": "Trace width 0.10mm < minimum 0.13mm",
                    "location": [10.0, 20.0],
                    "layer": "F.Cu",
                    "actual_value": 0.10,
                    "required_value": 0.13,
                    "items": ["Trace on F.Cu"],
                },
                {
                    "rule_id": "silkscreen_line_width",
                    "severity": "warning",
                    "message": "Silkscreen line width 0.10mm < minimum 0.15mm",
                    "location": [30.0, 40.0],
                    "layer": "F.SilkS",
                    "actual_value": 0.10,
                    "required_value": 0.15,
                    "items": [],
                },
                {
                    "rule_id": "clearance_pad_pad",
                    "severity": "error",
                    "message": "Pad to pad clearance 0.15mm < minimum 0.20mm",
                    "location": [50.0, 60.0],
                    "layer": "F.Cu",
                    "actual_value": 0.15,
                    "required_value": 0.20,
                    "items": ["Pad 1", "Pad 2"],
                },
            ],
        }

        report = _parse_kct_check_json(kct_json, "test.json")
        assert len(report.violations) == 3

        v0 = report.violations[0]
        assert v0.type == ViolationType.DIMENSION_TRACE_WIDTH
        assert v0.type_str == "dimension_trace_width"
        assert v0.rule == "dimension_trace_width"

        v1 = report.violations[1]
        assert v1.type == ViolationType.SILKSCREEN_LINE_WIDTH
        assert v1.type_str == "silkscreen_line_width"

        v2 = report.violations[2]
        assert v2.type == ViolationType.CLEARANCE_PAD_PAD
        assert v2.type_str == "clearance_pad_pad"

    def test_round_trip_no_unknown_types(self):
        """No violation from kct-check JSON should end up as UNKNOWN."""
        from kicad_tools.drc.report import _parse_kct_check_json

        rule_ids = [
            "clearance_pad_pad",
            "edge_clearance_trace",
            "dimension_trace_width",
            "silkscreen_line_width",
            "solder_mask_clearance",
            "impedance",
            "pth_annular_ring",
            "min_pad_size",
        ]
        kct_json = {
            "file": "/tmp/test.kicad_pcb",
            "manufacturer": "jlcpcb",
            "layers": 2,
            "summary": {
                "errors": len(rule_ids),
                "warnings": 0,
                "rules_checked": len(rule_ids),
                "passed": False,
            },
            "violations": [
                {
                    "rule_id": rid,
                    "severity": "error",
                    "message": f"Test violation for {rid}",
                    "location": [0, 0],
                    "layer": "F.Cu",
                    "items": [],
                }
                for rid in rule_ids
            ],
        }

        report = _parse_kct_check_json(kct_json, "test.json")
        for v in report.violations:
            assert v.type != ViolationType.UNKNOWN, (
                f"rule_id {v.type_str!r} resolved to UNKNOWN after round-trip"
            )


class TestExistingBehaviorPreserved:
    """Ensure existing KiCad-cli type strings still work after changes."""

    @pytest.mark.parametrize(
        "type_str, expected",
        [
            ("clearance", ViolationType.CLEARANCE),
            ("unconnected_items", ViolationType.UNCONNECTED_ITEMS),
            ("shorting_items", ViolationType.SHORTING_ITEMS),
            ("track_width", ViolationType.TRACK_WIDTH),
            ("via_annular_width", ViolationType.VIA_ANNULAR_WIDTH),
            ("drill_hole_too_small", ViolationType.DRILL_HOLE_TOO_SMALL),
            ("drill_clearance", ViolationType.DRILL_CLEARANCE),
            ("silk_over_copper", ViolationType.SILK_OVER_COPPER),
            ("silk_overlap", ViolationType.SILK_OVERLAP),
            ("solder_mask_bridge", ViolationType.SOLDER_MASK_BRIDGE),
            ("courtyard_overlap", ViolationType.COURTYARD_OVERLAP),
            ("copper_edge_clearance", ViolationType.COPPER_EDGE_CLEARANCE),
            ("footprint", ViolationType.FOOTPRINT),
            ("duplicate_footprint", ViolationType.DUPLICATE_FOOTPRINT),
            ("malformed_outline", ViolationType.MALFORMED_OUTLINE),
        ],
    )
    def test_kicad_cli_types_preserved(self, type_str: str, expected: ViolationType):
        assert ViolationType.from_string(type_str) == expected

    @pytest.mark.parametrize(
        "description, expected",
        [
            ("Track width too small", ViolationType.TRACK_WIDTH),
            ("Trace width too narrow", ViolationType.TRACK_WIDTH),
            ("edge clearance violation", ViolationType.COPPER_EDGE_CLEARANCE),
            ("Drill size too small", ViolationType.DRILL_HOLE_TOO_SMALL),
            ("Silkscreen overlap", ViolationType.SILK_OVERLAP),
            ("Solder mask bridge", ViolationType.SOLDER_MASK_BRIDGE),
        ],
    )
    def test_fuzzy_descriptions_preserved(self, description: str, expected: ViolationType):
        assert ViolationType.from_string(description) == expected

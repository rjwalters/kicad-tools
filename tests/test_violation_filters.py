"""Tests for the DRC/ERC violation filtering and reclassification engine."""

from __future__ import annotations

import textwrap

import pytest

from kicad_tools.core.types import ERCSeverity, Severity
from kicad_tools.drc.report import DRCReport
from kicad_tools.drc.violation import DRCViolation as DRCReportViolation
from kicad_tools.drc.violation import ViolationType
from kicad_tools.erc.report import ERCReport
from kicad_tools.erc.violation import ERCViolation, ERCViolationType
from kicad_tools.validate.filters import (
    FilterConfigError,
    FilterEngine,
    ViolationFilter,
    load_filters_from_toml,
    parse_filters_from_config,
)
from kicad_tools.validate.violations import DRCViolation as ValidateViolation

# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _drc_violation(
    type_str: str = "clearance",
    message: str = "Clearance violation",
    severity: Severity = Severity.ERROR,
    items: list[str] | None = None,
    nets: list[str] | None = None,
) -> DRCReportViolation:
    return DRCReportViolation(
        type=ViolationType.from_string(type_str),
        type_str=type_str,
        severity=severity,
        message=message,
        items=items or [],
        nets=nets or [],
    )


def _erc_violation(
    type_str: str = "pin_not_connected",
    description: str = "Pin 1 of U1 not connected",
    severity: ERCSeverity = ERCSeverity.ERROR,
    sheet: str = "/",
    items: list[str] | None = None,
) -> ERCViolation:
    return ERCViolation(
        type=ERCViolationType.from_string(type_str),
        type_str=type_str,
        severity=severity,
        description=description,
        sheet=sheet,
        items=items or [],
    )


def _validate_violation(
    rule_id: str = "clearance_pad_pad",
    message: str = "Pad clearance violation",
    severity: str = "error",
    items: tuple[str, ...] = (),
    nets: tuple[str, ...] = (),
) -> ValidateViolation:
    return ValidateViolation(
        rule_id=rule_id,
        severity=severity,
        message=message,
        items=items,
        nets=nets,
    )


# ---------------------------------------------------------------------------
# ViolationFilter construction
# ---------------------------------------------------------------------------


class TestViolationFilterConstruction:
    def test_default_action_is_ignore(self):
        f = ViolationFilter()
        assert f.action == "ignore"

    def test_valid_actions(self):
        for action in ("ignore", "warning", "error"):
            f = ViolationFilter(action=action)
            assert f.action == action

    def test_invalid_action_raises(self):
        with pytest.raises(FilterConfigError, match="Invalid filter action"):
            ViolationFilter(action="suppress")

    def test_invalid_regex_raises(self):
        with pytest.raises(FilterConfigError, match="Invalid regex"):
            ViolationFilter(type_pattern="[invalid")

    def test_all_patterns_compile(self):
        f = ViolationFilter(
            type_pattern="silk.*",
            message_pattern="overlap",
            component_pattern="^U\\d+$",
            net_pattern="GND|VCC",
            sheet_pattern="/sub",
        )
        assert len(f._compiled) == 5


# ---------------------------------------------------------------------------
# ViolationFilter.matches -- DRC violations
# ---------------------------------------------------------------------------


class TestDRCFilterMatching:
    def test_match_type_pattern(self):
        f = ViolationFilter(type_pattern="silk_overlap|silkscreen_over_pad")
        assert f.matches(_drc_violation(type_str="silk_overlap"))
        assert f.matches(_drc_violation(type_str="silkscreen_over_pad"))
        assert not f.matches(_drc_violation(type_str="clearance"))

    def test_match_message_pattern(self):
        f = ViolationFilter(message_pattern="netclass.*Default")
        v = _drc_violation(message="Clearance violation (netclass 'Default')")
        assert f.matches(v)
        assert not f.matches(_drc_violation(message="Via too small"))

    def test_match_component_pattern(self):
        f = ViolationFilter(component_pattern="^U3$")
        v = _drc_violation(items=["Pad 1 of U3 on F.Cu", "Via [GND]"])
        assert f.matches(v)
        # Different component
        v2 = _drc_violation(items=["Pad 1 of C1 on F.Cu"])
        assert not f.matches(v2)

    def test_match_net_pattern(self):
        f = ViolationFilter(net_pattern="SPI_.*")
        v = _drc_violation(nets=["SPI_MOSI", "SPI_CLK"])
        assert f.matches(v)
        v2 = _drc_violation(nets=["GND"])
        assert not f.matches(v2)

    def test_no_nets_fails_net_pattern(self):
        f = ViolationFilter(net_pattern="GND")
        v = _drc_violation(nets=[])
        assert not f.matches(v)

    def test_all_patterns_must_match(self):
        """When multiple patterns specified, all must match (AND logic)."""
        f = ViolationFilter(
            type_pattern="courtyard_overlap",
            component_pattern="^U1$",
        )
        # Both match
        v = _drc_violation(
            type_str="courtyard_overlap",
            items=["Pad 1 of U1", "Pad 2 of U2"],
        )
        assert f.matches(v)

        # Type matches but component doesn't
        v2 = _drc_violation(
            type_str="courtyard_overlap",
            items=["Pad 1 of C5"],
        )
        assert not f.matches(v2)

    def test_no_patterns_matches_everything(self):
        f = ViolationFilter()
        assert f.matches(_drc_violation())

    def test_case_insensitive(self):
        f = ViolationFilter(type_pattern="SILK_OVERLAP")
        assert f.matches(_drc_violation(type_str="silk_overlap"))


# ---------------------------------------------------------------------------
# ViolationFilter.matches -- ERC violations
# ---------------------------------------------------------------------------


class TestERCFilterMatching:
    def test_match_type_pattern(self):
        f = ViolationFilter(type_pattern="single_global_label")
        assert f.matches(_erc_violation(type_str="single_global_label"))
        assert not f.matches(_erc_violation(type_str="pin_not_connected"))

    def test_match_sheet_pattern(self):
        f = ViolationFilter(sheet_pattern="/power")
        assert f.matches(_erc_violation(sheet="/power"))
        assert not f.matches(_erc_violation(sheet="/analog"))

    def test_match_description_pattern(self):
        f = ViolationFilter(message_pattern="Pin 1 of U1")
        assert f.matches(_erc_violation(description="Pin 1 of U1 not connected"))
        assert not f.matches(_erc_violation(description="Pin 2 of C3 not driven"))


# ---------------------------------------------------------------------------
# ViolationFilter.matches -- Validate violations (frozen dataclass)
# ---------------------------------------------------------------------------


class TestValidateFilterMatching:
    def test_match_rule_id(self):
        f = ViolationFilter(type_pattern="clearance_pad_pad")
        assert f.matches(_validate_violation(rule_id="clearance_pad_pad"))
        assert not f.matches(_validate_violation(rule_id="track_width"))

    def test_match_items(self):
        f = ViolationFilter(component_pattern="^D1$")
        v = _validate_violation(items=("D1", "C5"))
        assert f.matches(v)

    def test_match_nets(self):
        f = ViolationFilter(net_pattern="VCC")
        v = _validate_violation(nets=("VCC", "GND"))
        assert f.matches(v)


# ---------------------------------------------------------------------------
# FilterEngine
# ---------------------------------------------------------------------------


class TestFilterEngine:
    def test_empty_filters_pass_all_through(self):
        engine = FilterEngine([])
        violations = [_drc_violation(), _drc_violation()]
        result = engine.apply(violations)
        assert result.kept_count == 2
        assert result.ignored_count == 0
        assert result.raw_count == 2

    def test_ignore_action(self):
        engine = FilterEngine(
            [
                ViolationFilter(type_pattern="silk_overlap", action="ignore"),
            ]
        )
        violations = [
            _drc_violation(type_str="silk_overlap"),
            _drc_violation(type_str="clearance"),
        ]
        result = engine.apply(violations)
        assert result.kept_count == 1
        assert result.ignored_count == 1
        assert result.kept[0].type_str == "clearance"

    def test_warning_reclassification(self):
        engine = FilterEngine(
            [
                ViolationFilter(type_pattern="courtyard_overlap", action="warning"),
            ]
        )
        v = _drc_violation(type_str="courtyard_overlap", severity=Severity.ERROR)
        result = engine.apply([v])
        assert result.kept_count == 1
        assert result.reclassified_count == 1
        assert result.kept[0].severity == Severity.WARNING

    def test_error_reclassification(self):
        engine = FilterEngine(
            [
                ViolationFilter(type_pattern="silk_overlap", action="error"),
            ]
        )
        v = _drc_violation(type_str="silk_overlap", severity=Severity.WARNING)
        result = engine.apply([v])
        assert result.kept_count == 1
        assert result.reclassified_count == 1
        assert result.kept[0].severity == Severity.ERROR

    def test_erc_severity_reclassification(self):
        engine = FilterEngine(
            [
                ViolationFilter(type_pattern="single_global_label", action="warning"),
            ]
        )
        v = _erc_violation(type_str="single_global_label", severity=ERCSeverity.ERROR)
        result = engine.apply([v])
        assert result.kept_count == 1
        assert result.reclassified_count == 1
        assert result.kept[0].severity == ERCSeverity.WARNING

    def test_validate_violation_reclassification(self):
        """Frozen ValidateViolation gets a new instance with changed severity."""
        engine = FilterEngine(
            [
                ViolationFilter(type_pattern="clearance_pad_pad", action="warning"),
            ]
        )
        v = _validate_violation(rule_id="clearance_pad_pad", severity="error")
        result = engine.apply([v])
        assert result.kept_count == 1
        assert result.kept[0].severity == "warning"
        # Original should be unchanged (frozen)
        assert v.severity == "error"

    def test_first_matching_rule_wins(self):
        engine = FilterEngine(
            [
                ViolationFilter(type_pattern="silk_overlap", action="ignore"),
                ViolationFilter(type_pattern="silk.*", action="warning"),
            ]
        )
        v = _drc_violation(type_str="silk_overlap")
        result = engine.apply([v])
        # First rule should win: ignore
        assert result.ignored_count == 1
        assert result.kept_count == 0

    def test_filter_result_counts(self):
        engine = FilterEngine(
            [
                ViolationFilter(type_pattern="silk_overlap", action="ignore"),
                ViolationFilter(type_pattern="courtyard", action="warning"),
            ]
        )
        violations = [
            _drc_violation(type_str="silk_overlap"),
            _drc_violation(type_str="courtyard_overlap"),
            _drc_violation(type_str="clearance"),
        ]
        result = engine.apply(violations)
        assert result.raw_count == 3
        assert result.kept_count == 2
        assert result.ignored_count == 1
        assert result.reclassified_count == 1

    def test_ignore_all_violations(self):
        engine = FilterEngine([ViolationFilter(action="ignore")])
        violations = [_drc_violation(), _drc_violation()]
        result = engine.apply(violations)
        assert result.kept_count == 0
        assert result.ignored_count == 2


# ---------------------------------------------------------------------------
# TOML config parsing
# ---------------------------------------------------------------------------


class TestConfigParsing:
    def test_parse_drc_filters(self):
        config = {
            "drc": {
                "filters": [
                    {
                        "type_pattern": "silk_overlap",
                        "action": "ignore",
                        "comment": "Cosmetic only",
                    },
                    {
                        "type_pattern": "courtyard_overlap",
                        "component_pattern": "^U1$",
                        "action": "warning",
                    },
                ]
            }
        }
        drc_filters, erc_filters = parse_filters_from_config(config)
        assert len(drc_filters) == 2
        assert len(erc_filters) == 0
        assert drc_filters[0].action == "ignore"
        assert drc_filters[1].component_pattern == "^U1$"

    def test_parse_erc_filters(self):
        config = {
            "erc": {
                "filters": [
                    {
                        "type_pattern": "single_global_label",
                        "action": "ignore",
                    }
                ]
            }
        }
        drc_filters, erc_filters = parse_filters_from_config(config)
        assert len(drc_filters) == 0
        assert len(erc_filters) == 1

    def test_parse_both_drc_and_erc(self):
        config = {
            "drc": {"filters": [{"type_pattern": "silk_overlap", "action": "ignore"}]},
            "erc": {"filters": [{"type_pattern": "single_global_label", "action": "ignore"}]},
        }
        drc_filters, erc_filters = parse_filters_from_config(config)
        assert len(drc_filters) == 1
        assert len(erc_filters) == 1

    def test_empty_config(self):
        drc_filters, erc_filters = parse_filters_from_config({})
        assert drc_filters == []
        assert erc_filters == []

    def test_invalid_action_in_config(self):
        config = {"drc": {"filters": [{"action": "discard"}]}}
        with pytest.raises(FilterConfigError):
            parse_filters_from_config(config)

    def test_invalid_regex_in_config(self):
        config = {"drc": {"filters": [{"type_pattern": "[bad regex"}]}}
        with pytest.raises(FilterConfigError):
            parse_filters_from_config(config)

    def test_default_action_is_ignore(self):
        config = {"drc": {"filters": [{"type_pattern": "silk_overlap"}]}}
        drc_filters, _ = parse_filters_from_config(config)
        assert drc_filters[0].action == "ignore"


# ---------------------------------------------------------------------------
# TOML file loading
# ---------------------------------------------------------------------------


class TestLoadFiltersFromToml:
    def test_load_valid_toml(self, tmp_path):
        toml_content = textwrap.dedent("""\
            [[drc.filters]]
            type_pattern = "silk_overlap"
            action = "ignore"
            comment = "Cosmetic"

            [[erc.filters]]
            type_pattern = "single_global_label"
            action = "ignore"
        """)
        config_file = tmp_path / "filters.toml"
        config_file.write_text(toml_content)

        drc_filters, erc_filters = load_filters_from_toml(str(config_file))
        assert len(drc_filters) == 1
        assert len(erc_filters) == 1
        assert drc_filters[0].comment == "Cosmetic"

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_filters_from_toml("/nonexistent/path.toml")

    def test_invalid_toml_syntax(self, tmp_path):
        config_file = tmp_path / "bad.toml"
        config_file.write_text("[[[[invalid")
        with pytest.raises(FilterConfigError):
            load_filters_from_toml(str(config_file))


# ---------------------------------------------------------------------------
# DRCReport.apply_filters integration
# ---------------------------------------------------------------------------


class TestDRCReportApplyFilters:
    def test_apply_filters_returns_new_report(self):
        report = DRCReport(
            source_file="test.kicad_pcb",
            created_at=None,
            pcb_name="test",
            violations=[
                _drc_violation(type_str="silk_overlap"),
                _drc_violation(type_str="clearance"),
            ],
        )
        filters = [ViolationFilter(type_pattern="silk_overlap", action="ignore")]
        filtered = report.apply_filters(filters)
        assert len(filtered.violations) == 1
        assert filtered.violations[0].type_str == "clearance"
        # Original report unchanged
        assert len(report.violations) == 2

    def test_empty_filters_no_change(self):
        report = DRCReport(
            source_file="test.kicad_pcb",
            created_at=None,
            pcb_name="test",
            violations=[_drc_violation()],
        )
        filtered = report.apply_filters([])
        assert len(filtered.violations) == 1


# ---------------------------------------------------------------------------
# ERCReport.apply_filters integration
# ---------------------------------------------------------------------------


class TestERCReportApplyFilters:
    def test_apply_filters_returns_new_report(self):
        report = ERCReport(
            source_file="test.kicad_sch",
            violations=[
                _erc_violation(type_str="single_global_label"),
                _erc_violation(type_str="pin_not_connected"),
            ],
        )
        filters = [ViolationFilter(type_pattern="single_global_label", action="ignore")]
        filtered = report.apply_filters(filters)
        assert len(filtered.violations) == 1
        assert filtered.violations[0].type_str == "pin_not_connected"
        # Original unchanged
        assert len(report.violations) == 2


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_no_component_refs_fails_component_filter(self):
        """If component_pattern is set but no refs found, filter does not match."""
        f = ViolationFilter(component_pattern="U1")
        v = _drc_violation(items=["Track on F.Cu"])  # No component ref
        assert not f.matches(v)

    def test_sheet_pattern_on_drc_violation_does_not_match(self):
        """sheet_pattern is ERC-only; DRC violations have no sheet."""
        f = ViolationFilter(sheet_pattern="/power")
        v = _drc_violation()
        assert not f.matches(v)

    def test_reclassify_does_not_mutate_original_drc_violation(self):
        """Reclassifying a DRC violation must not change the original object."""
        original = _drc_violation(type_str="courtyard_overlap", severity=Severity.ERROR)
        engine = FilterEngine(
            [
                ViolationFilter(type_pattern="courtyard_overlap", action="warning"),
            ]
        )
        result = engine.apply([original])
        assert result.kept[0].severity == Severity.WARNING
        # The original violation must still have ERROR severity
        assert original.severity == Severity.ERROR
        # The returned violation must be a different object
        assert result.kept[0] is not original

    def test_reclassify_does_not_mutate_original_erc_violation(self):
        """Reclassifying an ERC violation must not change the original object."""
        original = _erc_violation(type_str="single_global_label", severity=ERCSeverity.ERROR)
        engine = FilterEngine(
            [
                ViolationFilter(type_pattern="single_global_label", action="warning"),
            ]
        )
        result = engine.apply([original])
        assert result.kept[0].severity == ERCSeverity.WARNING
        # The original violation must still have ERROR severity
        assert original.severity == ERCSeverity.ERROR
        # The returned violation must be a different object
        assert result.kept[0] is not original

    def test_reclassify_does_not_mutate_original_in_report(self):
        """DRCReport.apply_filters must not mutate original report violations."""
        v = _drc_violation(type_str="courtyard_overlap", severity=Severity.ERROR)
        report = DRCReport(
            source_file="test.kicad_pcb",
            created_at=None,
            pcb_name="test",
            violations=[v],
        )
        filters = [ViolationFilter(type_pattern="courtyard_overlap", action="warning")]
        filtered = report.apply_filters(filters)
        # Filtered report has warning severity
        assert filtered.violations[0].severity == Severity.WARNING
        # Original report still has error severity
        assert report.violations[0].severity == Severity.ERROR

    def test_multiple_rules_ignore_all(self):
        engine = FilterEngine(
            [
                ViolationFilter(type_pattern="silk_overlap", action="ignore"),
                ViolationFilter(type_pattern="clearance", action="ignore"),
            ]
        )
        violations = [
            _drc_violation(type_str="silk_overlap"),
            _drc_violation(type_str="clearance"),
        ]
        result = engine.apply(violations)
        assert result.kept_count == 0
        assert result.ignored_count == 2

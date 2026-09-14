"""Regression coverage for electrically incompatible resistor suggestions (#5029)."""

from unittest.mock import Mock

import pytest

from kicad_tools.cost.suggest import PartSuggester, parse_component_value
from kicad_tools.parts.models import Part, SearchResult


def suggest(value, *parts):
    client = Mock()
    client.search.return_value = SearchResult(query=value, parts=list(parts))
    suggester = PartSuggester(min_stock=0)
    suggester._client = client
    return suggester.suggest_for_component("R1", value, "Resistor_SMD:R_0805_2012Metric"), client


def part(code, description, **kwargs):
    return Part(lcsc_part=code, description=description, package="0805", stock=20000, **kwargs)


def test_decimal_substring_collision_is_rejected_even_for_basic_stock():
    result, _ = suggest(
        "16k", part("Cwrong", "3.16kΩ 0805", is_basic=True), part("Cright", "16kΩ 0805")
    )
    assert [p.lcsc_part for p in result.suggestions] == ["Cright"]


@pytest.mark.parametrize("value", ["470k HV", "470k unusual-annotation", "470k 400V 0.75W"])
def test_annotations_preserve_resistance_and_report_unverified_requirements(value):
    parsed = parse_component_value(value, "R1")
    assert parsed.numeric_value == 470000
    result, client = suggest(value, part("Cwrong", "0Ω 200V 750mW"), part("Cright", "470kΩ"))
    assert "470k" in client.search.call_args.args[0]
    assert [p.lcsc_part for p in result.suggestions] == ["Cright"]
    assert result.warnings
    assert "not verified" in result.warnings[0]


@pytest.mark.parametrize("value", ["HV", "unknown", "16k/3.16k"])
def test_unresolved_values_do_not_search_by_package(value):
    result, client = suggest(value, part("Cwrong", "0Ω 0805"))
    client.search.assert_not_called()
    assert not result.suggestions
    assert result.error


@pytest.mark.parametrize(
    ("value", "description"),
    [("16k", "16000 Ohms 0805"), ("4K7", "4.7kΩ"), ("0R5", "500mΩ"), ("0R", "0Ω")],
)
def test_equivalent_resistance_units_match(value, description):
    result, _ = suggest(value, part("Cright", description))
    assert result.best_suggestion.lcsc_part == "Cright"


@pytest.mark.parametrize("description", ["FRC0805F3161TS", "0805 200V 750mW", "100nF capacitor"])
def test_unknown_resistance_does_not_become_high_confidence_match(description):
    result, _ = suggest("16k", part("Cunknown", description))
    assert not result.suggestions


def test_structured_value_is_used_and_close_but_distinct_nominals_are_rejected():
    result, _ = suggest(
        "16k", part("Cwrong", "resistor", value="16.2k"), part("Cright", "resistor", value="16k")
    )
    assert [p.lcsc_part for p in result.suggestions] == ["Cright"]


def test_conflicting_structured_and_description_values_are_rejected():
    result, _ = suggest("16k", part("Cwrong", "3.16kΩ", value="16k"))
    assert not result.suggestions


def test_annotation_warning_is_visible_in_both_cli_json_formats(capsys):
    import json

    from kicad_tools.cli.commands.parts import _print_json_result
    from kicad_tools.cli.parts_cmd import _suggest_json
    from kicad_tools.cost.suggest import SuggestionResult

    suggestion, _ = suggest("470k HV", part("Cright", "470kΩ"))
    assert suggestion.best_suggestion.confidence <= 0.75
    result = SuggestionResult(suggestions=[suggestion])
    _print_json_result(result, active_references=1, missing_lcsc_references=1)
    assert json.loads(capsys.readouterr().out)["suggestions"][0]["warnings"]
    _suggest_json(result, result.suggestions)
    assert json.loads(capsys.readouterr().out)["suggestions"][0]["warnings"]


def test_structured_milliohm_value_matches_inline_decimal_request():
    result, _ = suggest("0R5", part("Cright", "resistor", value="500mΩ"))
    assert result.best_suggestion is not None
    assert result.best_suggestion.lcsc_part == "Cright"

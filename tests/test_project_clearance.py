"""Electrical netclass precedence must survive manufacturer/tier plumbing."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from kicad_tools.core.project_clearance import (
    NetclassClearance,
    ProjectClearanceError,
    resolve_project_clearances,
)


@pytest.mark.parametrize(
    "case",
    json.loads((Path(__file__).parent / "fixtures/project_clearance/oracle.json").read_text())[
        "cases"
    ],
    ids=lambda case: case["id"],
)
def test_clearance_matches_native_cli_project_loading(case):
    actual = resolve_project_clearances(case["project"], case["expected_clearances"])
    assert {name: result.clearance for name, result in actual.items()} == case[
        "expected_clearances"
    ]


def project(version=5):
    return {
        "net_settings": {
            "meta": {"version": version},
            "classes": [
                {"name": "Default", "clearance": 0.2},
                {"name": "Low", "clearance": 0.1, "priority": 0},
                {"name": "High", "clearance": 0.4, "priority": 1},
                {"name": "Inherited", "priority": -2},
            ],
            "netclass_patterns": [{"pattern": "USB*", "netclass": "High"}],
            "netclass_assignments": {"USB1": ["Low", "Inherited"]},
        }
    }


def test_priority_overrides_maximum_and_default_without_mutating_input():
    data = project()
    before = deepcopy(data)
    assert resolve_project_clearances(data, ["USB1", "USB2", "Other", ""]) == {
        "USB1": NetclassClearance(0.1, "Low"),
        "USB2": NetclassClearance(0.4, "High"),
        "Other": NetclassClearance(0.2, "Default"),
    }
    assert data == before


def test_inherited_clearance_uses_next_class_then_default():
    data = project()
    data["net_settings"]["netclass_assignments"] = {"USB1": ["Inherited"], "Other": ["Inherited"]}
    result = resolve_project_clearances(data, ["USB1", "Other"])
    assert result["USB1"] == NetclassClearance(0.4, "High")
    assert result["Other"] == NetclassClearance(0.2, "Default")


def test_priority_tie_is_alphabetic_not_largest_or_assignment_order():
    data = project()
    data["net_settings"]["classes"][2]["priority"] = 0
    assert resolve_project_clearances(data, ["USB1"])["USB1"] == NetclassClearance(0.4, "High")


def test_schema_three_migrates_order_and_string_assignments_privately():
    data = project(3)
    data["net_settings"]["classes"][1]["priority"] = 100
    data["net_settings"]["netclass_assignments"] = {"USB1": "Low", "Other": ""}
    before = deepcopy(data)
    assert resolve_project_clearances(data, ["USB1"])["USB1"] == NetclassClearance(0.1, "Low")
    assert data == before


def test_implicit_default_can_be_assigned_and_zero_is_explicit():
    data = project()
    data["net_settings"]["classes"] = [{"name": "Zero", "clearance": 0.0}]
    data["net_settings"]["netclass_patterns"] = []
    data["net_settings"]["netclass_assignments"] = {"A": ["Zero"], "B": ["Default"]}
    assert resolve_project_clearances(data, ["A", "B"]) == {
        "A": NetclassClearance(0.0, "Zero"),
        "B": NetclassClearance(0.2, "Default"),
    }


@pytest.mark.parametrize("value", [True, -0.1, float("nan"), float("inf"), "0.2"])
def test_invalid_electrical_values_are_not_silently_dropped(value):
    data = project()
    data["net_settings"]["classes"][1]["clearance"] = value
    with pytest.raises(ProjectClearanceError, match="Invalid clearance"):
        resolve_project_clearances(data, ["USB1"])


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s.update(meta={"version": 6}),
        lambda s: s.update(netclass_patterns=[{"pattern": "USB[0-9]", "netclass": "High"}]),
        lambda s: s.update(netclass_assignments={"USB1": ["Missing"]}),
        lambda s: s["classes"].append({"name": "Low", "clearance": 0.7}),
        lambda s: s["classes"][0].pop("clearance"),
        lambda s: s["classes"][1].update(priority=True),
    ],
)
def test_unsupported_or_ambiguous_rules_fail_explicitly(mutation):
    data = project()
    mutation(data["net_settings"])
    with pytest.raises(ProjectClearanceError):
        resolve_project_clearances(data, ["USB1"])


def test_missing_settings_is_distinct_from_implicit_default():
    assert resolve_project_clearances({}, ["A"]) == {}
    assert resolve_project_clearances({"net_settings": {"meta": {"version": 5}}}, ["A"]) == {
        "A": NetclassClearance(0.2, "Default")
    }

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
        "": NetclassClearance(0.2, "Default"),
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


def test_authored_netclass_clearances_are_every_non_default_class():
    from kicad_tools.core.project_clearance import authored_netclass_clearances

    data = project()
    result = authored_netclass_clearances(data, ["USB1", "USB2", "Other"])
    # USB1 resolves to Low, USB2 to High; Other is Default and owned by the base.
    assert result == {
        "USB1": NetclassClearance(0.1, "Low"),
        "USB2": NetclassClearance(0.4, "High"),
    }


@pytest.mark.parametrize(
    "settings",
    [
        {"classes": [{"name": "Default", "clearance": 0.15}]},  # no meta.version at all
        {"meta": {"version": 99}, "classes": [{"name": "Default", "clearance": 0.15}]},
        {"meta": {"version": 3}, "classes": [{"name": "Default"}, {"name": "Bare"}]},
    ],
)
def test_default_only_projects_never_reach_the_strict_resolver(settings):
    from kicad_tools.core.project_clearance import authored_netclass_clearances

    assert authored_netclass_clearances({"net_settings": settings}, ["A"]) == {}


def test_named_classes_still_fail_explicitly_on_unsupported_schema():
    from kicad_tools.core.project_clearance import authored_netclass_clearances

    data = project()
    data["net_settings"]["meta"] = {"version": 6}
    with pytest.raises(ProjectClearanceError, match="Unsupported net_settings schema"):
        authored_netclass_clearances(data, ["USB1"])


# ---------------------------------------------------------------------------
# Issue #6262: a project without a modellable schema STATEMENT is not an
# unmodellable project.  Absent/null net_settings means "no authored
# netclasses"; an unversioned block is read as the current schema, exactly as
# KiCad's NESTED_SETTINGS loader does (kicad-cli 10 enforces the HV class of
# such a block -- see the hv-class-* scenarios in
# tests/test_route_auto_pair_and_hole_clearance_6122_6139.py).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        {},  # legacy/minimal .kicad_pro: no net_settings key at all
        {"meta": {"filename": "board.kicad_pro", "version": 1}, "board": {}},
        {"net_settings": None},  # JSON null
    ],
    ids=["no-key", "minimal-project", "null"],
)
def test_absent_or_null_net_settings_means_no_authored_netclasses(data):
    from kicad_tools.core.project_clearance import authored_netclass_clearances

    assert resolve_project_clearances(data, ["A", ""]) == {}
    assert authored_netclass_clearances(data, ["A"]) == {}


@pytest.mark.parametrize(
    "settings",
    [
        {},  # empty block: KiCad's built-in Default only
        {"meta": {}},
        {"meta": None},
        {"meta": {"version": None}},
    ],
    ids=["empty", "meta-without-version", "null-meta", "null-version"],
)
def test_unversioned_empty_block_resolves_to_default_without_raising(settings):
    from kicad_tools.core.project_clearance import authored_netclass_clearances

    data = {"net_settings": settings}
    assert resolve_project_clearances(data, ["A"]) == {"A": NetclassClearance(0.2, "Default")}
    assert authored_netclass_clearances(data, ["A"]) == {}


@pytest.mark.parametrize("meta", [None, {}, {"version": None}], ids=["no-meta", "empty", "null"])
def test_unversioned_named_classes_are_read_as_the_current_schema(meta):
    from kicad_tools.core.project_clearance import authored_netclass_clearances

    data = project()
    if meta is None:
        del data["net_settings"]["meta"]
    else:
        data["net_settings"]["meta"] = meta
    expected = resolve_project_clearances(project(), ["USB1", "USB2", "Other"])
    assert resolve_project_clearances(data, ["USB1", "USB2", "Other"]) == expected
    assert authored_netclass_clearances(data, ["USB1", "USB2", "Other"]) == {
        "USB1": NetclassClearance(0.1, "Low"),
        "USB2": NetclassClearance(0.4, "High"),
    }


@pytest.mark.parametrize(
    "settings",
    [
        {"meta": {"version": 2}},  # stated but needs a migration we do not model
        {"meta": {"version": "5"}},  # stated but not an integer
        {"meta": 5},  # meta present but not an object
    ],
)
def test_a_stated_but_unsupported_schema_still_raises(settings):
    data = project()
    data["net_settings"].update(settings)
    with pytest.raises(ProjectClearanceError, match="Unsupported net_settings schema|meta must"):
        resolve_project_clearances(data, ["USB1"])


@pytest.mark.parametrize("settings", [[], "HV", 0])
def test_a_present_non_object_net_settings_still_raises(settings):
    with pytest.raises(ProjectClearanceError, match="net_settings must be an object"):
        resolve_project_clearances({"net_settings": settings}, ["A"])


@pytest.mark.parametrize(
    ("project_data", "expected"),
    [
        ({"meta": {"filename": "b.kicad_pro", "version": 1}}, {}),
        ({"net_settings": None}, {}),
        ({"net_settings": {}}, {}),
        (
            {
                "net_settings": {
                    "classes": [
                        {"name": "Default", "clearance": 0.1},
                        {"name": "HV", "clearance": 0.5},
                    ],
                    "netclass_patterns": [{"netclass": "HV", "pattern": "/Power/*"}],
                }
            },
            {"/Power/VIN": 0.5},
        ),
    ],
    ids=["minimal", "null", "empty", "unversioned-hierarchical"],
)
def test_router_load_path_reads_projects_without_a_schema_statement(
    tmp_path, project_data, expected
):
    """``kct route``/route-auto's loader, the caller #6252 broke (#6262)."""
    from kicad_tools.router.io import _authored_net_clearances

    pcb = tmp_path / "b.kicad_pcb"
    pcb.with_suffix(".kicad_pro").write_text(json.dumps(project_data))
    nets = {"/Power/VIN": 1, "/SIG": 2}
    assert _authored_net_clearances(pcb, None, nets) == expected

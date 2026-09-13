"""Ambiguous operating data must never erase a failing stress state."""

from pathlib import Path

import pytest

from kicad_tools.analysis.component_stress import OperatingState, OperatingStateManifest
from kicad_tools.cli.analyze_cmd import main

FIXTURE = Path(__file__).parent / "fixtures/component_stress/mosfets.kicad_sch"


@pytest.mark.parametrize(
    "first,second", [("mains-negative", "mains_negative"), ("Startup", "startup")]
)
def test_alias_states_cannot_overwrite_unsafe_state(first, second):
    with pytest.raises(ValueError, match="duplicate.*state"):
        OperatingStateManifest.from_dict({"states": {first: {"D": 259}, second: {"D": 0}}})


@pytest.mark.parametrize("nets", [{"D": 259, "/d": 0}, {"/D": 259, "d": 0}])
def test_ambiguous_net_declarations_are_rejected(nets):
    with pytest.raises(ValueError, match="ambiguous.*net"):
        OperatingStateManifest.from_dict({"states": {"startup": nets}})


@pytest.mark.parametrize("required", [[], [""], ["---"], ["startup", "Startup"]])
def test_invalid_coverage_cannot_disable_census(required):
    with pytest.raises(ValueError):
        OperatingStateManifest.from_dict({"required_states": required, "states": {}})


@pytest.mark.parametrize(
    "extension,text",
    [
        ("json", '{"states":{"startup":{"D":259},"startup":{"D":0}}}'),
        ("json", '{"states":{"startup":{"D":259,"D":0}}}'),
        ("yaml", "states:\n  startup: {D: 259}\n  startup: {D: 0}\n"),
        ("yaml", "states:\n  startup:\n    D: 259\n    D: 0\n"),
    ],
)
def test_serialized_duplicates_fail_cli_without_census(tmp_path, capsys, extension, text):
    path = tmp_path / f"states.{extension}"
    path.write_text(text)
    before = FIXTURE.read_bytes()
    assert main(["component-stress", str(FIXTURE), "--states", str(path), "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert "duplicate" in captured.err
    assert not captured.out
    assert FIXTURE.read_bytes() == before


def test_direct_empty_manifest_still_has_missing_coverage():
    assert OperatingStateManifest(states={}, required_states=()).coverage_states()


def test_mutated_state_ambiguity_is_unresolved():
    state = OperatingState(name="startup", raw_name="startup", potentials={"D": 259, "/d": 0})
    assert state.potential("D") is None
    assert state.potential("d") is None


def test_normalized_unique_controls_still_load(tmp_path):
    path = tmp_path / "states.yaml"
    path.write_text(
        "required_states: [Mains-Negative]\nstates:\n  mains_negative: {D: 259, S: 0}\n"
    )
    manifest = OperatingStateManifest.load(path)
    assert manifest.get("mains negative").potential("/d") == 259
    assert manifest.coverage_states() == ["mains_negative"]


@pytest.mark.parametrize(
    "state", [{"D": 259, "nets": {"D": 0, "S": 0}}, {"nets": []}, {"nets": None}]
)
def test_mixed_or_malformed_nets_are_not_silently_discarded(state):
    with pytest.raises(ValueError):
        OperatingStateManifest.from_dict({"states": {"startup": state}})

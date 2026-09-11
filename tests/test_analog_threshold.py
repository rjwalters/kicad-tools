"""Correlated analog thresholds and source-bound, fail-visible sidecars."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from kicad_tools.analysis.analog_threshold import analyze_thresholds, evaluate_network, main

FIXTURE = Path(__file__).parent / "fixtures/analog_threshold/softstart_partial.json"


@pytest.fixture
def network():
    return json.loads(FIXTURE.read_text())


def test_documented_partial_fixture(network):
    result = evaluate_network(network)
    assert result["vertices"] == 4096
    rising, falling = result["thresholds"]["rising"], result["thresholds"]["falling"]
    assert rising["nominal_v"] == pytest.approx(70.24398, abs=0.00001)
    assert falling["nominal_v"] == pytest.approx(60.33902, abs=0.00001)
    assert (rising["sample_min_v"], rising["sample_max_v"]) == pytest.approx(
        (67.66329, 72.83212), abs=0.00001
    )
    assert (falling["sample_min_v"], falling["sample_max_v"]) == pytest.approx(
        (57.96032, 62.72502), abs=0.00001
    )
    assert result["status"] == "failed"
    assert result["coverage"] == "incomplete"
    assert result["omitted_terms"]
    assert not result["release_eligible"]


def bounded(network):
    network["unmodeled_terms"] = []
    network["conditions"] = {
        "temperature_c": {"min": 25, "nominal": 25, "max": 25, "source": "ideal fixture"},
        "linear_operation": "explicit ideal fixture",
    }
    for key in (
        "amplifier_offset",
        "buffer_offset",
        "comparator_offset",
        "output_low",
        "output_high_drop",
    ):
        network["bounds"][key] = {
            "min": 0,
            "nominal": 0,
            "max": 0,
            "source": "Explicit ideal fixture assumption",
        }
    network["requirements"]["falling"]["min"] = 50
    return network


def test_bounded_vertices_are_never_promoted_to_proof(network):
    result = evaluate_network(bounded(network))
    assert result["status"] == "sampled"
    assert result["bound_method"] == "vertex_sampling_not_a_proven_enclosure"
    assert not result["release_eligible"]


def test_missing_error_bound_cannot_qualify(network):
    network["requirements"]["falling"]["min"] = 50
    result = evaluate_network(network)
    assert result["status"] == "incomplete"
    assert any("amplifier_offset" in text for text in result["omitted_terms"])


def test_common_mode_translation_and_shared_supply(network):
    # Matched ideal resistor ratios reject arbitrary input common-mode.
    for parts in network["resistors"].values():
        for part in parts:
            part["tolerance"] = 0
    baseline = evaluate_network(bounded(network))
    network["bounds"]["common_mode"] = {
        "min": 200,
        "nominal": 200,
        "max": 200,
        "source": "Translated terminals",
    }
    moved = evaluate_network(network)
    for edge in ("rising", "falling", "hysteresis"):
        assert moved["thresholds"][edge]["sample_min_v"] == pytest.approx(
            baseline["thresholds"][edge]["sample_min_v"]
        )
        assert moved["thresholds"][edge]["sample_max_v"] == pytest.approx(
            baseline["thresholds"][edge]["sample_max_v"]
        )
    # Same supply sets reference, midpoint AND comparator high output.
    expected = (10000 / 665000) / (10000 / 1996000)
    hyst = moved["thresholds"]["hysteresis"]
    assert hyst["sample_min_v"] == pytest.approx(3.234 * expected)
    assert hyst["sample_max_v"] == pytest.approx(3.366 * expected)


def test_offset_and_output_swing_change_thresholds(network):
    ideal = evaluate_network(bounded(network))
    network["bounds"]["comparator_offset"] = {
        "min": 0.001,
        "nominal": 0.001,
        "max": 0.001,
        "source": "Fixture input offset",
    }
    offset = evaluate_network(network)
    assert offset["thresholds"]["rising"]["nominal_v"] > ideal["thresholds"]["rising"]["nominal_v"]
    network["bounds"]["output_high_drop"] = {
        "min": 0.2,
        "nominal": 0.2,
        "max": 0.2,
        "source": "Fixture high-level drop",
    }
    swing = evaluate_network(network)
    assert (
        swing["thresholds"]["falling"]["nominal_v"] > offset["thresholds"]["falling"]["nominal_v"]
    )
    assert (
        swing["thresholds"]["hysteresis"]["nominal_v"]
        < offset["thresholds"]["hysteresis"]["nominal_v"]
    )


def divider():
    return {
        "id": "divider",
        "conditions": {
            "temperature_c": {"min": 25, "nominal": 25, "max": 25, "source": "ideal fixture"},
            "linear_operation": "ideal test model",
        },
        "kind": "divider",
        "resistors": {
            "top": [{"reference": "R1", "ohms": 100, "tolerance": 0.01, "source": "fixture"}],
            "bottom": [{"reference": "R2", "ohms": 220, "tolerance": 0.01, "source": "fixture"}],
        },
        "bounds": {
            "reference": {"min": 1, "nominal": 1, "max": 1, "source": "fixture"},
            "comparator_offset": {"min": 0, "nominal": 0, "max": 0, "source": "fixture"},
        },
        "requirements": {"rising": {"max": 2, "source": "fixture"}},
        "devices": {"comparator": "D1"},
    }


def test_simple_divider():
    result = evaluate_network(divider())
    assert result["thresholds"]["rising"]["nominal_v"] == pytest.approx(1 + 100 / 220)
    assert result["thresholds"]["rising"]["sample_max_v"] == pytest.approx(1 + 101 / 217.8)
    assert result["thresholds"]["hysteresis"]["sample_max_v"] == 0


@pytest.mark.parametrize("bad", [-1, float("nan"), True, "0.1"])
def test_invalid_tolerance_rejected(network, bad):
    network["resistors"]["signal"][0]["tolerance"] = bad
    with pytest.raises(ValueError):
        evaluate_network(network)


def test_duplicate_role_and_missing_source(network):
    network["resistors"]["signal"] = copy.deepcopy(network["resistors"]["feedback"])
    with pytest.raises(ValueError, match="Duplicate"):
        evaluate_network(network)


@pytest.fixture
def bound_files(tmp_path):
    # This validates identity/connectivity binding, not the physical topology of
    # the divider template. The topology itself is explicitly reviewed input.
    from kicad_tools.operations.netlist import build_netlist_from_schematic
    from kicad_tools.schema.schematic import Schematic
    from kicad_tools.sexp import SExp, parse_file

    source = Path(__file__).parent / "fixtures/electrical_rating/leds.kicad_sch"
    tree = parse_file(source)
    for symbol in tree.find_all("symbol"):
        reference = next(
            (
                p.get_string(1)
                for p in symbol.find_all("property")
                if p.get_string(0) == "Reference"
            ),
            None,
        )
        if reference in {"R1", "R2", "D1"}:
            symbol.children.append(SExp.list("property", "MPN", f"FIXTURE-{reference}"))
    path = tmp_path / "bound.kicad_sch"
    path.write_text(tree.to_string())
    sch = Schematic.load(path)
    nets = build_netlist_from_schematic(path)
    bindings = {}
    for symbol in sch.symbols:
        if symbol.reference not in {"R1", "R2", "D1"}:
            continue
        bindings[symbol.reference] = {
            "value": symbol.value,
            "mpn": f"FIXTURE-{symbol.reference}",
            "pins": {
                node.pin: net.name
                for net in nets.nets
                for node in net.nodes
                if node.reference == symbol.reference
            },
        }
    policy = {
        "schema_version": 1,
        "schematic_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bindings": bindings,
        "networks": [divider()],
    }
    sidecar = tmp_path / "thresholds.json"
    sidecar.write_text(json.dumps(policy))
    return path, sidecar, policy


def test_source_bound_report_and_cli(bound_files, capsys):
    path, sidecar, _ = bound_files
    report = analyze_thresholds(path, sidecar)
    assert report["status"] == "sampled", report
    assert report["policy_sha256"] == hashlib.sha256(sidecar.read_bytes()).hexdigest()
    assert main([str(path), str(sidecar)]) == 2
    assert not json.loads(capsys.readouterr().out)["release_eligible"]


@pytest.mark.parametrize("field", ["value", "mpn", "pins"])
def test_binding_mismatch_is_incomplete(bound_files, field):
    path, sidecar, policy = bound_files
    policy["bindings"]["R1"][field] = {"1": "wrong"} if field == "pins" else "changed"
    sidecar.write_text(json.dumps(policy))
    result = analyze_thresholds(path, sidecar)
    assert result["status"] == "incomplete" and result["issues"]
    assert not result["networks"]


def test_source_hash_change_invalidates_bindings(bound_files):
    path, sidecar, _ = bound_files
    path.write_text(path.read_text() + "\n")
    result = analyze_thresholds(path, sidecar)
    assert result["status"] == "incomplete"
    assert "Stale" in result["issues"][0]


def test_missing_operating_condition_is_incomplete(network):
    bounded(network)
    del network["conditions"]["temperature_c"]
    result = evaluate_network(network)
    assert result["status"] == "incomplete"
    assert any("temperature" in item for item in result["omitted_terms"])


def test_missing_resistor_tolerance_is_incomplete(network):
    bounded(network)
    del network["resistors"]["signal"][0]["tolerance"]
    assert evaluate_network(network)["status"] == "incomplete"


def test_bad_policy_is_json_incomplete(tmp_path, capsys):
    source, policy = tmp_path / "test.kicad_sch", tmp_path / "policy.json"
    source.write_text("(kicad_sch)")
    policy.write_text("[]")
    assert main([str(source), str(policy)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "incomplete"


def test_failed_declared_requirement_cli(bound_files, capsys):
    path, sidecar, policy = bound_files
    policy["networks"][0]["requirements"]["rising"]["max"] = 1
    sidecar.write_text(json.dumps(policy))
    assert main([str(path), str(sidecar)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "failed"
    assert result["networks"][0]["source_binding"] == "verified"

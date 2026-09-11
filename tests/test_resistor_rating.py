"""Source-bound resistor budgets: physical controls, missing evidence and CLI."""

import copy
import hashlib
import json

import pytest

from kicad_tools.analysis.resistor_rating import Budget, evaluate_budget, main


@pytest.fixture
def budget(tmp_path):
    def evidence(name, data):
        (tmp_path / name).write_text(data)
        return {"path": name, "sha256": hashlib.sha256(data.encode()).hexdigest()}

    symbols = "".join(
        f'(symbol (lib_id "Device:R") (at 0 0 0) (uuid "r{i}") '
        f'(property "Reference" "R{i}") (property "Value" "10") '
        '(property "MPN" "reviewed-part"))'
        for i in range(1, 11)
    )
    sch = evidence(
        "board.kicad_sch", '(kicad_sch (version 20250114) (generator "test") ' + symbols + ")"
    )
    proof = evidence(
        "conditions.txt", "Reviewed ambient at 25 C; mounting/interface installation record."
    )
    source = {
        "source": "user-reviewed fixture, not manufacturer qualification",
        "reviewed_by": "engineer",
        "reviewed_on": "2026-09-10",
    }
    state = {
        "kind": "continuous",
        "samples": [{"duration_s": 1, "voltage_v": 3}],
        "temperature_basis": "ambient",
        "temperature_c": 25,
        "temperature_evidence": proof,
        "assumptions": ["constant 3V, switch drop ignored"],
    }
    part = {
        "reference": "R1",
        "mpn": "reviewed-part",
        "resistance_ohm": 10,
        "tolerance_fraction": 0.01,
        "ratings": [
            {
                "basis": "ambient",
                "power_w": 2,
                "derating": [[0, 1], [70, 1], [150, 0]],
                "requires_mounting": False,
                "provenance": source,
            }
        ],
        "states": {"active": state},
    }
    data = {
        "schema_version": 1,
        "schematic": sch,
        "operating_policy": evidence(
            "policy.txt", "active selected; historical zero-bank startup excluded"
        ),
        "active_states": ["active"],
        "resistors": [part],
        "simultaneous_groups": {},
    }
    path = tmp_path / "rating.json"

    def run(value=None):
        path.write_text(json.dumps(data if value is None else value))
        return evaluate_budget(path)

    return data, run, path, proof


def test_ten_tolerance_channels_have_aggregate_heat_without_temperature_prediction(budget):
    data, run, _, _ = budget
    template = data["resistors"][0]
    data["resistors"] = [dict(copy.deepcopy(template), reference=f"R{i}") for i in range(1, 11)]
    data["simultaneous_groups"] = {"balance": [f"R{i}" for i in range(1, 11)]}
    report = run()
    assert report["status"] == "pass"
    assert all(r["average_power_w"] == pytest.approx(0.909090909) for r in report["rows"])
    assert report["simultaneous_groups"][0]["average_power_w"] == pytest.approx(9.09090909)
    assert report["simultaneous_groups"][0]["temperature_prediction"] is None


def test_nameplate_100w_does_not_replace_mounting_or_interface_evidence(budget):
    data, run, _, proof = budget
    rating = data["resistors"][0]["ratings"][0]
    rating.update(basis="flange", power_w=100, requires_mounting=True)
    state = data["resistors"][0]["states"]["active"]
    state["temperature_basis"] = "flange"
    assert run()["status"] == "incomplete"
    state["mounting_evidence"] = proof
    assert run()["status"] == "incomplete"
    state["interface_evidence"] = proof
    assert run()["status"] == "pass"
    state["temperature_basis"] = "ambient"
    assert run()["status"] == "incomplete"


def test_derating_range_and_interpolation(budget):
    data, run, _, _ = budget
    state = data["resistors"][0]["states"]["active"]
    state["temperature_c"] = 110
    assert run()["rows"][0]["ratings"][0]["allowed_power_w"] == 1
    state["temperature_c"] = 130
    assert run()["status"] == "fail"
    state["temperature_c"] = -10
    assert run()["status"] == "incomplete"


def test_average_safe_pulse_exceeding_energy_fails(budget):
    data, run, _, _ = budget
    part = data["resistors"][0]
    state = part["states"]["active"]
    state.update(kind="periodic", samples=[{"duration_s": 0.01, "voltage_v": 10}], period_s=1)
    report = run()
    assert report["status"] == "incomplete"
    assert report["rows"][0]["average_power_w"] < 2
    part["pulse_limits"] = [
        {
            "duration_max_s": 0.02,
            "peak_max_w": 11,
            "energy_max_j": 0.05,
            "repetition_max_hz": 1,
            "temperature_max_c": 70,
            "basis": "ambient",
            "provenance": part["ratings"][0]["provenance"],
        }
    ]
    report = run()
    assert report["status"] == "fail"
    assert "pulse energy exceeds envelope" in report["rows"][0]["ratings"][0]["failures"]
    part["pulse_limits"][0]["energy_max_j"] = 0.2
    assert run()["status"] == "pass"
    part["pulse_limits"][0]["repetition_max_hz"] = 0.5
    assert run()["status"] == "fail"


def test_piecewise_constant_waveform_integral_and_rms(budget):
    data, run, _, _ = budget
    state = data["resistors"][0]["states"]["active"]
    state.update(
        kind="periodic",
        samples=[{"duration_s": 1, "power_w": 4}, {"duration_s": 1, "power_w": 0}],
        period_s=4,
        reviewed_power_includes_tolerance=True,
    )
    row = run()["rows"][0]
    assert row["window_energy_j"] == 4
    assert row["average_power_w"] == 1
    assert row["rms_power_w"] == 2
    assert row["duty_fraction"] == 0.5
    assert row["repetition_hz"] == 0.25


def test_current_drive_uses_high_resistance_tolerance(budget):
    data, run, _, _ = budget
    data["resistors"][0]["states"]["active"]["samples"] = [{"duration_s": 1, "current_a": 0.3}]
    assert run()["rows"][0]["average_power_w"] == pytest.approx(0.909)


@pytest.mark.parametrize("target", ["board.kicad_sch", "policy.txt", "conditions.txt"])
def test_stale_evidence_never_passes(budget, target):
    _, run, path, _ = budget
    (path.parent / target).write_text("changed")
    assert run()["status"] == "incomplete"


def test_mpn_change_and_state_selection_are_explicit(budget):
    data, run, _, _ = budget
    part = data["resistors"][0]
    historical = copy.deepcopy(part["states"]["active"])
    historical["samples"][0]["voltage_v"] = 1000
    part["states"]["historical_zero_bank"] = historical
    assert run()["status"] == "pass"
    data["active_states"] = ["historical_zero_bank"]
    assert run()["status"] == "fail"
    data["active_states"] = ["not_declared"]
    assert run()["status"] == "incomplete"
    data["active_states"] = ["active"]
    part["mpn"] = "different-part"
    assert run()["status"] == "incomplete"


def test_reviewed_dissipation_requires_tolerance_attestation(budget):
    data, run, _, _ = budget
    state = data["resistors"][0]["states"]["active"]
    state["samples"] = [{"duration_s": 1, "power_w": 0.5}]
    assert run()["status"] == "incomplete"
    state["reviewed_power_includes_tolerance"] = True
    assert run()["status"] == "pass"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(active_states=[]),
        lambda d: d.update(simultaneous_groups={"x": ["R1", "R1"]}),
        lambda d: d["resistors"][0].update(tolerance_fraction=1),
        lambda d: d["resistors"][0].update(resistance_ohm=float("nan")),
        lambda d: d["resistors"][0]["states"]["active"]["samples"][0].update(current_a=1),
        lambda d: d["resistors"][0]["states"]["active"].update(kind="periodic", period_s=0.5),
        lambda d: d["resistors"][0]["ratings"][0].update(derating=[[70, 1], [0, 0]]),
    ],
)
def test_malformed_policy_is_rejected(budget, mutation):
    data, _, _, _ = budget
    mutation(data)
    with pytest.raises(ValueError):
        Budget.model_validate(data)


def test_cli_exit_codes_and_invalid_schema(budget, capsys):
    data, run, path, _ = budget
    run()
    assert main([str(path)]) == 0
    data["resistors"][0]["mpn"] = "wrong"
    run()
    assert main([str(path)]) == 2
    data["resistors"][0]["mpn"] = "reviewed-part"
    data["resistors"][0]["states"]["active"]["samples"][0]["voltage_v"] = 100
    run()
    assert main([str(path)]) == 1
    path.write_text("{bad")
    assert main([str(path)]) == 2
    assert "incomplete" in capsys.readouterr().out


def test_wrong_temperature_basis_does_not_invent_derating_failure(budget):
    data, run, _, _ = budget
    state = data["resistors"][0]["states"]["active"]
    state.update(temperature_basis="flange", temperature_c=149)
    report = run()
    assert report["status"] == "incomplete"
    assert report["rows"][0]["ratings"][0]["allowed_power_w"] is None


def test_sidecar_binding_changes_and_evidence_path_escape(budget, tmp_path):
    data, run, _, _ = budget
    first = run()
    data["resistors"][0]["states"]["active"]["assumptions"].append("additional reviewed assumption")
    assert first["sidecar_sha256"] != run()["sidecar_sha256"]
    data["operating_policy"]["path"] = "../policy.txt"
    assert run()["status"] == "incomplete"


def test_numeric_overflow_and_string_review_attestation_are_not_passes(budget):
    data, run, path, _ = budget
    data["resistors"][0]["states"]["active"]["samples"][0]["voltage_v"] = 1e308
    with pytest.raises(ValueError):
        run()
    assert main([str(path)]) == 2
    data["resistors"][0]["states"]["active"]["samples"][0]["voltage_v"] = 3
    data["resistors"][0]["states"]["active"]["reviewed_power_includes_tolerance"] = "false"
    with pytest.raises(ValueError):
        run()

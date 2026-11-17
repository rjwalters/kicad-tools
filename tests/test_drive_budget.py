"""Independent arithmetic and fail-visible evidence controls for drive budgets."""

import json
import math

import pytest

from kicad_tools.analysis.drive_budget import (
    DriveBudget,
    analyze_drive_budget,
    crossing_window,
    main,
)


def quantity(value, basis="nominal", part="reviewed-part"):
    return {
        "value": value,
        "basis": basis,
        "part": part,
        "source": "Explicit synthetic operating-condition fixture",
    }


@pytest.fixture
def data():
    return {
        "schema_version": 1,
        "topology": "emitter_follower",
        "state": "hold",
        "target_drive_v": 12,
        "duration_s": 0.008,
        "source_current_cap_a": quantity(15e-6, part="U10"),
        "source_voltage_cap_v": quantity(8.4, part="U10"),
        "bias_resistance_ohm": quantity(100e3, part="R46"),
        "follower_drop_v": quantity(0, part="Q9"),
        "bleeder_resistance_ohm": quantity(10e3, part="R_GS"),
        "reservoir_initial_v": quantity(12, part="C_RES"),
        "reservoir_effective_f": quantity(10e-6, part="C_RES"),
        "gate_charge_c": quantity(0, part="Q_SWITCH"),
        "additional_static_a": quantity(0),
        "assumptions": ["Optimistically ignore base current"],
        "unmodeled_effects": ["MLCC effective capacitance must be reviewed at operating bias"],
        "crossing": {"rms_voltage_v": 120, "frequency_hz": 60, "threshold_v": 10},
    }


def analyze(data):
    return analyze_drive_budget(DriveBudget.model_validate_json(json.dumps(data)))


def test_photovoltaic_current_booster_cannot_supply_voltage_gain(data):
    report = analyze(data)
    assert report["nominal_metrics"]["optimistic_base_v"] == pytest.approx(1.5)
    assert report["nominal_metrics"]["optimistic_drive_v"] == pytest.approx(1.5)
    assert report["nominal_metrics"]["bias_current_at_target_a"] == pytest.approx(120e-6)
    assert report["nominal_shortfalls"]
    assert report["status"] == "incomplete"  # typical is never a guaranteed limit
    assert report["guaranteed_drive_ceiling_v"] is None
    data["reservoir_initial_v"]["value"] = 100
    assert analyze(data)["nominal_metrics"]["optimistic_drive_v"] == pytest.approx(1.5)
    data["bias_resistance_ohm"]["value"] = 1e9
    assert analyze(data)["nominal_metrics"]["optimistic_drive_v"] == 8.4


def test_bleeder_remains_loaded_after_switching_edge(data):
    metrics = analyze(data)["nominal_metrics"]
    assert metrics["bleeder_current_at_target_a"] == pytest.approx(0.0012)
    assert metrics["ideal_rc_end_v"] == pytest.approx(12 * math.exp(-0.08))
    assert metrics["ideal_rc_droop_v"] == pytest.approx(0.92260384)
    assert metrics["constant_target_load_end_v"] == pytest.approx(11.04)
    assert metrics["constant_target_charge_demand_c"] == pytest.approx(9.6e-6)
    data["gate_charge_c"]["value"] = 1e-6
    data["additional_static_a"]["value"] = 0.0001
    changed = analyze(data)["nominal_metrics"]
    assert changed["gate_charge_step_v"] == pytest.approx(0.1)
    assert changed["ideal_rc_end_v"] == pytest.approx((11.9 + 1) * math.exp(-0.08) - 1)
    assert changed["ideal_rc_end_v"] < metrics["ideal_rc_end_v"]


def test_sine_crossing_full_window_not_quarter_window(data):
    report = analyze(data)["crossing"]
    assert report["window_s"] == pytest.approx(0.00031278, rel=0.001)
    assert report["windows_per_cycle"] == 2
    assert report["cycle_fraction"] == pytest.approx(report["window_s"] * 60 * 2)
    assert crossing_window(120, 60, 0)["window_s"] == 0
    assert crossing_window(120, 60, 200)["window_s"] == pytest.approx(1 / 60)
    assert crossing_window(120, 60, 200)["windows_per_cycle"] == 1


def test_only_correct_bound_directions_prove_voltage_shortfall(data):
    data["source_current_cap_a"]["basis"] = "upper_bound"
    data["bias_resistance_ohm"]["basis"] = "upper_bound"
    report = analyze(data)
    assert report["status"] == "fail"
    assert report["guaranteed_drive_ceiling_v"] == pytest.approx(1.5)
    data["source_current_cap_a"]["basis"] = "lower_bound"  # ISC minimum is NOT an upper bound
    assert analyze(data)["status"] == "incomplete"
    data["source_voltage_cap_v"]["basis"] = "upper_bound"
    assert analyze(data)["guaranteed_drive_ceiling_v"] == 8.4


def test_wrong_drop_bound_is_not_used_to_prove_failure(data):
    data["target_drive_v"] = 8
    data["source_voltage_cap_v"]["basis"] = "upper_bound"
    data["follower_drop_v"] = quantity(1, "upper_bound")
    assert analyze(data)["status"] == "incomplete"
    data["follower_drop_v"]["basis"] = "lower_bound"
    assert analyze(data)["status"] == "fail"


def test_unknown_topology_cannot_inherit_follower_verdict(data):
    data["topology"] = "nonlinear_boost_converter"
    data["source_voltage_cap_v"]["basis"] = "upper_bound"
    report = analyze(data)
    assert report["status"] == "incomplete"
    assert report["nominal_metrics"]["optimistic_drive_v"] is None
    assert any("Unsupported" in x for x in report["unmodeled_effects"])


def test_excess_gate_charge_exhausts_reservoir_without_negative_voltage(data):
    data["gate_charge_c"]["value"] = 1
    report = analyze(data)
    assert report["nominal_metrics"]["gate_charge_exceeds_initial_reservoir"]
    assert report["nominal_metrics"]["ideal_rc_end_v"] == 0
    assert report["nominal_metrics"]["constant_target_load_end_v"] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("reservoir_effective_f", 0),
        ("bleeder_resistance_ohm", 0),
        ("source_current_cap_a", float("nan")),
        ("source_voltage_cap_v", 1e308),
        ("reservoir_effective_f", 1e-300),
    ],
)
def test_invalid_budget_cannot_return_green(data, field, value):
    data[field]["value"] = value
    with pytest.raises(ValueError):
        analyze(data)


def test_successful_ceiling_screen_does_not_prove_loaded_operating_point(data):
    data["source_current_cap_a"]["value"] = 0.1
    data["source_voltage_cap_v"]["value"] = 20
    data["source_voltage_cap_v"]["basis"] = "upper_bound"
    report = analyze(data)
    assert report["status"] == "incomplete"
    assert report["guaranteed_drive_ceiling_v"] == 20
    assert any("loaded operating voltage" in item for item in report["unmodeled_effects"])


def test_cli_preserves_input_and_provenance(data, tmp_path, capsys):
    path = tmp_path / "drive.json"
    path.write_text(json.dumps(data))
    raw = path.read_bytes()
    assert main([str(path)]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["quantities"]["source_current_cap_a"]["part"] == "U10"
    assert len(result["sidecar_sha256"]) == 64
    assert path.read_bytes() == raw
    data["source_voltage_cap_v"]["basis"] = "upper_bound"
    path.write_text(json.dumps(data))
    assert main([str(path)]) == 1

"""Explicit weak-source and ideal gate-reservoir budget review (#5041).

These checks can disprove a drive target; sufficient current gain, switching
behavior and nonlinear source loading still require simulation or review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Quantity(Record):
    value: float = Field(ge=0, le=1e18)
    basis: Literal["nominal", "upper_bound", "lower_bound"]
    part: str = Field(min_length=1)
    source: str = Field(min_length=1)

    @model_validator(mode="after")
    def numeric_range(self) -> Quantity:
        if 0 < self.value < 1e-18:
            raise ValueError("nonzero quantities must be at least 1e-18")
        return self


class Crossing(Record):
    rms_voltage_v: float = Field(gt=1e-18, le=1e18)
    frequency_hz: float = Field(gt=1e-18, le=1e18)
    threshold_v: float = Field(ge=0, le=1e18)


class DriveBudget(Record):
    schema_version: Literal[1]
    topology: str = Field(min_length=1)
    state: str = Field(min_length=1)
    target_drive_v: float = Field(gt=0, le=1e18)
    duration_s: float = Field(ge=0, le=1e18)
    source_current_cap_a: Quantity
    source_voltage_cap_v: Quantity
    bias_resistance_ohm: Quantity
    follower_drop_v: Quantity
    bleeder_resistance_ohm: Quantity
    reservoir_initial_v: Quantity
    reservoir_effective_f: Quantity
    gate_charge_c: Quantity
    additional_static_a: Quantity
    assumptions: list[str] = Field(min_length=1)
    unmodeled_effects: list[str]
    crossing: Crossing | None = None

    @model_validator(mode="after")
    def positive_divisors(self) -> DriveBudget:
        for name in ("bias_resistance_ohm", "bleeder_resistance_ohm", "reservoir_effective_f"):
            if getattr(self, name).value == 0:
                raise ValueError(f"{name} must be positive")
        if 0 < self.duration_s < 1e-18 or self.target_drive_v < 1e-18:
            raise ValueError("nonzero policy quantities must be at least 1e-18")
        return self


def crossing_window(
    rms_voltage_v: float, frequency_hz: float, threshold_v: float
) -> dict[str, float]:
    """Full interval around ONE zero crossing, and fraction of the whole cycle."""
    data = Crossing(rms_voltage_v=rms_voltage_v, frequency_hz=frequency_hz, threshold_v=threshold_v)
    ratio = data.threshold_v / (data.rms_voltage_v * math.sqrt(2))
    if ratio >= 1:
        return {"window_s": 1 / data.frequency_hz, "cycle_fraction": 1.0, "windows_per_cycle": 1.0}
    angle = math.asin(ratio)
    return {
        "window_s": angle / (math.pi * data.frequency_hz),
        "cycle_fraction": 2 * angle / math.pi,
        "windows_per_cycle": 2.0,
    }


def analyze_drive_budget(budget: DriveBudget) -> dict[str, Any]:
    """Report nominal estimates and directionally valid bounds separately.

    No successful upper-bound screen proves the operating point is achievable.
    Reservoir estimates use a declared ideal RC load, instant gate-charge draw,
    and constant additional load; they are never a switching qualification.
    """
    q = budget
    optimistic_base = min(
        q.source_voltage_cap_v.value, q.source_current_cap_a.value * q.bias_resistance_ohm.value
    )
    optimistic_drive = max(0.0, optimistic_base - q.follower_drop_v.value)
    bleeder_a = q.target_drive_v / q.bleeder_resistance_ohm.value
    charge_step_v = q.gate_charge_c.value / q.reservoir_effective_f.value
    initial_after_charge = max(0.0, q.reservoir_initial_v.value - charge_step_v)
    resistance = q.bleeder_resistance_ohm.value
    capacitance = q.reservoir_effective_f.value
    static = q.additional_static_a.value
    decay_loss = -math.expm1(-q.duration_s / (resistance * capacitance))
    # expm1 avoids cancellation when t << RC. Clamp after computing losses;
    # an exhausted reservoir is reported explicitly, not modeled negative.
    loss = (initial_after_charge + static * resistance) * decay_loss
    end_v = max(0.0, initial_after_charge - loss)
    constant_load_end = max(
        0.0, initial_after_charge - (bleeder_a + static) * q.duration_s / capacitance
    )
    missing = [
        "Upper-bound source screening does not establish available current at the loaded operating voltage",
        "Transistor base current, minimum current gain, collector headroom and switching transients require review",
    ]
    failures = []
    nominal_shortfalls = []
    supported = q.topology == "emitter_follower"
    if not supported:
        missing.append(f"Unsupported nonlinear topology: {q.topology}")
    elif optimistic_drive < q.target_drive_v:
        nominal_shortfalls.append(
            "Optimistic emitter-follower drive voltage below target; collector reservoir provides no voltage gain"
        )
    guaranteed_candidates = []
    if (
        q.source_current_cap_a.basis == "upper_bound"
        and q.bias_resistance_ohm.basis == "upper_bound"
    ):
        guaranteed_candidates.append(q.source_current_cap_a.value * q.bias_resistance_ohm.value)
    if q.source_voltage_cap_v.basis == "upper_bound":
        guaranteed_candidates.append(q.source_voltage_cap_v.value)
    guaranteed_drive_ceiling = None
    if supported and guaranteed_candidates:
        # Without a guaranteed minimum drop, zero is the optimistic passive drop.
        drop = q.follower_drop_v.value if q.follower_drop_v.basis == "lower_bound" else 0.0
        guaranteed_drive_ceiling = max(0.0, min(guaranteed_candidates) - drop)
        if guaranteed_drive_ceiling < q.target_drive_v:
            failures.append("Guaranteed optimistic voltage ceiling is below required drive")
    if end_v < q.target_drive_v:
        nominal_shortfalls.append("Ideal RC reservoir voltage falls below target before state ends")
    if constant_load_end < q.target_drive_v:
        nominal_shortfalls.append(
            "Constant target-voltage bleeder model falls below target before state ends"
        )
    required_bias_a = (q.target_drive_v + q.follower_drop_v.value) / q.bias_resistance_ohm.value
    charge_demand = q.gate_charge_c.value + (bleeder_a + static) * q.duration_s
    available_charge = max(0.0, q.reservoir_initial_v.value - q.target_drive_v) * capacitance
    if supported and required_bias_a > q.source_current_cap_a.value:
        nominal_shortfalls.append(
            "Bias current required at target exceeds declared source current ceiling"
        )
    if charge_demand > available_charge:
        nominal_shortfalls.append(
            "Gate plus static-load charge exceeds reservoir charge available above target"
        )
    quantities = {
        name: getattr(q, name).model_dump()
        for name in (
            "source_current_cap_a",
            "source_voltage_cap_v",
            "bias_resistance_ohm",
            "follower_drop_v",
            "bleeder_resistance_ohm",
            "reservoir_initial_v",
            "reservoir_effective_f",
            "gate_charge_c",
            "additional_static_a",
        )
    }
    metrics = {
        "optimistic_base_v": optimistic_base,
        "optimistic_drive_v": optimistic_drive if supported else None,
        "bleeder_current_at_target_a": bleeder_a,
        "bias_current_at_target_a": required_bias_a,
        "nominal_source_current_headroom_a": q.source_current_cap_a.value - required_bias_a,
        "constant_target_charge_demand_c": charge_demand,
        "reservoir_charge_available_above_target_c": available_charge,
        "gate_charge_step_v": charge_step_v,
        "ideal_rc_end_v": end_v,
        "ideal_rc_droop_v": q.reservoir_initial_v.value - end_v,
        "constant_target_load_end_v": constant_load_end,
        "gate_charge_exceeds_initial_reservoir": charge_step_v > q.reservoir_initial_v.value,
    }
    if not all(math.isfinite(v) for v in metrics.values() if isinstance(v, (int, float))):
        raise ValueError("budget arithmetic outside supported range")
    return {
        "schema_version": 1,
        "state": q.state,
        "topology": q.topology,
        "status": "fail" if failures else "incomplete",
        "guaranteed_failures": failures,
        "guaranteed_drive_ceiling_v": guaranteed_drive_ceiling,
        "nominal_shortfalls": nominal_shortfalls,
        "nominal_metrics": metrics,
        "quantities": quantities,
        "assumptions": q.assumptions,
        "unmodeled_effects": missing + q.unmodeled_effects,
        "crossing": crossing_window(**q.crossing.model_dump()) if q.crossing else None,
        "scope": "Review of declared weak-source ceilings and ideal reservoir budgets; never manufacturing approval",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sidecar", type=Path)
    args = parser.parse_args(argv)
    try:
        raw = args.sidecar.read_bytes()
        result = analyze_drive_budget(DriveBudget.model_validate_json(raw))
        result["sidecar_sha256"] = hashlib.sha256(raw).hexdigest()
        print(json.dumps(result, indent=2, allow_nan=False))
    except (OSError, ValueError, OverflowError) as exc:
        print(json.dumps({"status": "incomplete", "error": str(exc)}))
        return 2
    return 1 if result["status"] == "fail" else 2


if __name__ == "__main__":
    raise SystemExit(main())

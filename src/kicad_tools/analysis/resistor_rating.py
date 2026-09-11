"""Evaluate explicit, source-bound resistor operating budgets (not thermal simulation).

Run ``python -m kicad_tools.analysis.resistor_rating budget.json``. Ratings and
operating conditions must be supplied and reviewed by the caller; nothing is
inferred from package names or nominal nameplate power.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kicad_tools.schema.schematic import Schematic


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


class Evidence(Record):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class Provenance(Record):
    source: str = Field(min_length=1)
    reviewed_by: str = Field(min_length=1)
    reviewed_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class Rating(Record):
    basis: Literal["ambient", "terminal", "flange"]
    power_w: float = Field(gt=0)
    # Piecewise-linear [temperature C, fraction of power_w]. No extrapolation.
    derating: list[tuple[float, float]] = Field(min_length=2)
    requires_mounting: bool
    provenance: Provenance

    @model_validator(mode="after")
    def valid_curve(self) -> Rating:
        if any(not 0 <= fraction <= 1 for _, fraction in self.derating):
            raise ValueError("derating fractions must be between zero and one")
        if any(
            b[0] <= a[0] or b[1] > a[1]
            for a, b in zip(self.derating, self.derating[1:], strict=False)
        ):
            raise ValueError("derating temperatures must increase and fractions not increase")
        return self


class PulseLimit(Record):
    """Reviewed envelope applicable to this precise rating and temperature basis."""

    duration_max_s: float = Field(gt=0)
    peak_max_w: float = Field(gt=0)
    energy_max_j: float = Field(gt=0)
    repetition_max_hz: float = Field(ge=0)
    temperature_max_c: float
    basis: Literal["ambient", "terminal", "flange"]
    provenance: Provenance


class Sample(Record):
    duration_s: float = Field(gt=0)
    voltage_v: float | None = None
    current_a: float | None = None
    power_w: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def one_quantity(self) -> Sample:
        if sum(v is not None for v in (self.voltage_v, self.current_a, self.power_w)) != 1:
            raise ValueError("each interval requires exactly one of voltage_v/current_a/power_w")
        return self


class OperatingState(Record):
    kind: Literal["continuous", "single_pulse", "periodic"]
    samples: list[Sample] = Field(min_length=1)
    # Samples are piecewise constant; periodic unsampled remainder is explicitly off.
    period_s: float | None = Field(default=None, gt=0)
    temperature_basis: Literal["ambient", "terminal", "flange"] | None = None
    temperature_c: float | None = None
    temperature_evidence: Evidence | None = None
    mounting_evidence: Evidence | None = None
    interface_evidence: Evidence | None = None
    reviewed_power_includes_tolerance: bool = False
    assumptions: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_period(self) -> OperatingState:
        duration = sum(s.duration_s for s in self.samples)
        if self.kind == "periodic":
            if self.period_s is None or self.period_s < duration:
                raise ValueError("periodic period_s must cover every sample interval")
        elif self.period_s is not None:
            raise ValueError("period_s is only valid for periodic states")
        if self.kind == "continuous" and len(self.samples) != 1:
            raise ValueError("continuous state requires one constant interval")
        return self


class Resistor(Record):
    reference: str = Field(min_length=1)
    mpn: str = Field(min_length=1)
    resistance_ohm: float = Field(gt=0)
    tolerance_fraction: float = Field(ge=0, lt=1)
    ratings: list[Rating] = Field(min_length=1)
    pulse_limits: list[PulseLimit] = Field(default_factory=list)
    states: dict[str, OperatingState]


class Budget(Record):
    schema_version: Literal[1]
    schematic: Evidence
    operating_policy: Evidence
    active_states: list[str] = Field(min_length=1)
    resistors: list[Resistor] = Field(min_length=1)
    simultaneous_groups: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_subjects(self) -> Budget:
        refs = [p.reference for p in self.resistors]
        if len(refs) != len(set(refs)) or len(self.active_states) != len(set(self.active_states)):
            raise ValueError("references and active states must be unique")
        for members in self.simultaneous_groups.values():
            if not members or len(set(members)) != len(members) or not set(members) <= set(refs):
                raise ValueError("groups require unique, declared resistor references")
        return self


def _bound_path(evidence: Evidence, base: Path) -> Path:
    path = (base / evidence.path).resolve()
    if Path(evidence.path).is_absolute() or not path.is_relative_to(base.resolve()):
        raise ValueError(f"evidence must remain relative to the sidecar: {evidence.path}")
    if hashlib.sha256(path.read_bytes()).hexdigest() != evidence.sha256:
        raise ValueError(f"stale evidence: {evidence.path}")
    return path


def _status(failures: list[str], missing: list[str]) -> str:
    return "fail" if failures else "incomplete" if missing else "pass"


def _power(part: Resistor, state: OperatingState) -> dict[str, float]:
    powers = []
    for sample in state.samples:
        if sample.voltage_v is not None:
            power = sample.voltage_v**2 / (part.resistance_ohm * (1 - part.tolerance_fraction))
        elif sample.current_a is not None:
            power = sample.current_a**2 * part.resistance_ohm * (1 + part.tolerance_fraction)
        else:
            assert sample.power_w is not None
            power = sample.power_w
        powers.append(power)
    duration = sum(s.duration_s for s in state.samples)
    period = state.period_s if state.period_s is not None else duration
    energy = sum(p * s.duration_s for p, s in zip(powers, state.samples, strict=True))
    if (
        not all(math.isfinite(p) for p in powers)
        or not math.isfinite(energy)
        or not math.isfinite(period)
    ):
        raise ValueError("non-finite power calculation")
    return {
        "peak_power_w": max(powers),
        "average_power_w": energy / period,
        "rms_power_w": math.sqrt(
            sum(p * p * s.duration_s for p, s in zip(powers, state.samples, strict=True)) / period
        ),
        "window_energy_j": energy,
        "duration_s": duration,
        "duty_fraction": duration / period,
        "repetition_hz": 1 / period if state.kind == "periodic" else 0,
    }


def _rating_power(rating: Rating, temperature: float) -> float | None:
    if not rating.derating[0][0] <= temperature <= rating.derating[-1][0]:
        return None
    for (ta, fa), (tb, fb) in zip(rating.derating, rating.derating[1:], strict=False):
        if ta <= temperature <= tb:
            return rating.power_w * (fa + (fb - fa) * (temperature - ta) / (tb - ta))
    return None


def evaluate_budget(sidecar: str | Path) -> dict[str, Any]:
    """Return pass/fail/incomplete with hashes and per-state, per-rating evidence.

    A pass applies only to supplied rating envelopes and selected states. It
    does not establish a heatsink design, aggregate temperature or bench result.
    Malformed schemas raise ValueError; missing/stale evidence returns incomplete.
    """
    path = Path(sidecar)
    raw = path.read_bytes()
    budget = Budget.model_validate_json(raw)
    base = path.parent
    missing: list[str] = []
    identities: dict[str, str | None] = {}
    try:
        schematic_path = _bound_path(budget.schematic, base)
        sch = Schematic.load(schematic_path)
        for symbol in sch.symbols:
            if symbol.reference in identities:
                raise ValueError(f"ambiguous schematic reference: {symbol.reference}")
            identities[symbol.reference] = symbol.get_property("MPN")
        _bound_path(budget.operating_policy, base)
    except (OSError, ValueError) as exc:
        missing.append(str(exc))
    rows: list[dict[str, Any]] = []
    for part in budget.resistors:
        binding_missing = list(missing)
        if identities.get(part.reference) != part.mpn:
            binding_missing.append(f"schematic MPN missing or mismatched for {part.reference}")
        for state_name in budget.active_states:
            state = part.states.get(state_name)
            common_missing = list(binding_missing)
            row: dict[str, Any] = {
                "reference": part.reference,
                "mpn": part.mpn,
                "state": state_name,
            }
            if state is None:
                row.update(status="incomplete", missing=common_missing + ["selected state missing"])
                rows.append(row)
                continue
            try:
                powers = _power(part, state)
            except (OverflowError, ZeroDivisionError) as exc:
                raise ValueError("power calculation outside finite numeric range") from exc
            if not all(math.isfinite(v) for v in powers.values()):
                raise ValueError("power calculation outside finite numeric range")
            row.update(powers)
            row["assumptions"] = state.assumptions
            if (
                any(s.power_w is not None for s in state.samples)
                and not state.reviewed_power_includes_tolerance
            ):
                common_missing.append("reviewed dissipation does not declare tolerance coverage")
            evidence_ok = {}
            for name in ("temperature_evidence", "mounting_evidence", "interface_evidence"):
                evidence = getattr(state, name)
                evidence_ok[name] = False
                if evidence is not None:
                    try:
                        _bound_path(evidence, base)
                        evidence_ok[name] = True
                    except (OSError, ValueError) as exc:
                        common_missing.append(str(exc))
            ratings = []
            for rating in part.ratings:
                absent = list(common_missing)
                failures: list[str] = []
                if state.temperature_basis != rating.basis:
                    absent.append("temperature basis does not match rating basis")
                if state.temperature_c is None or not evidence_ok["temperature_evidence"]:
                    absent.append(f"{rating.basis} temperature evidence missing")
                if rating.requires_mounting or rating.basis in ("terminal", "flange"):
                    for name in ("mounting_evidence", "interface_evidence"):
                        if not evidence_ok[name]:
                            absent.append(name + " missing")
                limit = (
                    None
                    if state.temperature_c is None or state.temperature_basis != rating.basis
                    else _rating_power(rating, state.temperature_c)
                )
                if limit is None:
                    absent.append("temperature outside rated curve or unknown")
                elif powers["average_power_w"] > limit:
                    failures.append("average power exceeds derated rating")
                if state.kind != "continuous":
                    envelopes = [p for p in part.pulse_limits if p.basis == rating.basis]
                    if not envelopes:
                        absent.append("applicable pulse envelope missing")
                    for pulse in envelopes:
                        if (
                            state.temperature_c is None
                            or state.temperature_c > pulse.temperature_max_c
                        ):
                            absent.append("pulse temperature applicability not established")
                            continue
                        for measured, maximum, label in [
                            (powers["duration_s"], pulse.duration_max_s, "pulse duration"),
                            (powers["peak_power_w"], pulse.peak_max_w, "pulse peak power"),
                            (powers["window_energy_j"], pulse.energy_max_j, "pulse energy"),
                            (powers["repetition_hz"], pulse.repetition_max_hz, "pulse repetition"),
                        ]:
                            if measured > maximum:
                                failures.append(label + " exceeds envelope")
                ratings.append(
                    {
                        "basis": rating.basis,
                        "provenance": rating.provenance.model_dump(),
                        "allowed_power_w": limit,
                        "status": _status(failures, absent),
                        "failures": failures,
                        "missing": absent,
                    }
                )
            row["ratings"] = ratings
            statuses = [r["status"] for r in ratings]
            row["status"] = (
                "fail"
                if "fail" in statuses
                else "incomplete"
                if "incomplete" in statuses
                else "pass"
            )
            rows.append(row)
    groups = []
    for state_name in budget.active_states:
        for name, refs in budget.simultaneous_groups.items():
            members = [r for r in rows if r["state"] == state_name and r["reference"] in refs]
            complete = all("average_power_w" in r for r in members)
            groups.append(
                {
                    "group": name,
                    "state": state_name,
                    "references": refs,
                    "average_power_w": sum(r["average_power_w"] for r in members)
                    if complete
                    else None,
                    "coincident_peak_upper_bound_w": sum(r["peak_power_w"] for r in members)
                    if complete
                    else None,
                    "temperature_prediction": None,
                }
            )
    statuses = [r["status"] for r in rows]
    return {
        "schema_version": 1,
        "status": "fail"
        if "fail" in statuses
        else "incomplete"
        if "incomplete" in statuses
        else "pass",
        "sidecar_sha256": hashlib.sha256(raw).hexdigest(),
        "bindings": {
            "schematic": budget.schematic.model_dump(),
            "operating_policy": budget.operating_policy.model_dump(),
        },
        "active_states": budget.active_states,
        "rows": rows,
        "simultaneous_groups": groups,
        "scope": "Supplied rating envelopes only; no aggregate temperature or hardware qualification.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sidecar", type=Path)
    args = parser.parse_args(argv)
    try:
        report = evaluate_budget(args.sidecar)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "incomplete", "error": str(exc)}))
        return 2
    print(json.dumps(report, indent=2, allow_nan=False))
    return {"pass": 0, "fail": 1, "incomplete": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())

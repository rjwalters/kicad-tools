"""Reviewed analog-network threshold budgets; vertex samples are not proofs.

No topology inference or semiconductor guarantees. Run as a module with a flat
schematic and JSON policy to obtain source-bound evidence and nonzero incomplete
or failed status. Mathematical helpers also support unbound fixture evaluation.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

DIFFERENCE_ROLES = (
    "input_positive",
    "input_negative",
    "feedback",
    "reference_arm",
    "midpoint_top",
    "midpoint_bottom",
    "comparator_top",
    "comparator_bottom",
    "signal",
    "hysteresis",
)
DIFFERENCE_BOUNDS = (
    "supply",
    "common_mode",
    "amplifier_offset",
    "buffer_offset",
    "comparator_offset",
    "output_low",
    "output_high_drop",
)
MAX_VERTICES = 262144


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f"{name}: finite number required")
    return float(value)


def _range(value: dict[str, Any], name: str) -> tuple[float, float, float]:
    low, nominal, high = (_number(value[key], name) for key in ("min", "nominal", "max"))
    if not low <= nominal <= high:
        raise ValueError(f"{name}: require min <= nominal <= max")
    return low, nominal, high


def _thresholds(kind: str, p: dict[str, Any]) -> tuple[Any, Any]:
    if kind == "divider":
        trip = (p["reference"] + p["comparator_offset"]) * (1 + p["top"] / p["bottom"])
        return trip, trip
    # Difference input = Vpositive - Vnegative. Common mode = their average.
    positive_gain = (
        (1 + p["feedback"] / p["input_negative"])
        * p["reference_arm"]
        / (p["input_positive"] + p["reference_arm"])
    )
    negative_gain = p["feedback"] / p["input_negative"]
    differential_gain = (positive_gain + negative_gain) / 2
    midpoint = (
        p["supply"] * p["midpoint_bottom"] / (p["midpoint_top"] + p["midpoint_bottom"])
        + p["buffer_offset"]
    )
    baseline = (positive_gain - negative_gain) * p["common_mode"] + (1 + negative_gain) * p[
        "input_positive"
    ] / (p["input_positive"] + p["reference_arm"]) * midpoint
    baseline += (1 + negative_gain) * p["amplifier_offset"]
    reference = (p["supply"] / p["comparator_top"] + midpoint / p["comparator_bottom"]) / (
        1 / p["comparator_top"] + 1 / p["comparator_bottom"]
    )
    reference += p["comparator_offset"]
    ratio = p["signal"] / p["hysteresis"]
    # Output high shares the *same* supply sample; do not independently sweep it.
    rising = (reference * (1 + ratio) - p["output_low"] * ratio - baseline) / differential_gain
    falling = (
        reference * (1 + ratio) - (p["supply"] - p["output_high_drop"]) * ratio - baseline
    ) / differential_gain
    return rising, falling


def evaluate_network(network: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a declared template without claiming schematic binding or proof."""
    kind = network["kind"]
    if kind not in {"divider", "difference_comparator"}:
        raise ValueError(f"Unsupported network kind: {kind}")
    roles = ("top", "bottom") if kind == "divider" else DIFFERENCE_ROLES
    required_bounds = ("reference", "comparator_offset") if kind == "divider" else DIFFERENCE_BOUNDS
    parameters: dict[str, tuple[float, float, float]] = {}
    omitted = list(network.get("unmodeled_terms", []))
    conditions = network.get("conditions", {})
    if "temperature_c" not in conditions:
        omitted.append("Operating temperature range absent")
    else:
        _range(conditions["temperature_c"], "temperature_c")
        if not conditions["temperature_c"].get("source"):
            omitted.append("Temperature range provenance absent")
    if not conditions.get("linear_operation"):
        omitted.append("Semiconductor linear-operation/common-mode qualification absent")
    references: list[str] = []
    provenance: dict[str, Any] = {}
    for role in roles:
        parts = network["resistors"][role]
        if not isinstance(parts, list) or not parts:
            raise ValueError(f"{role}: nonempty series resistor list required")
        low = nominal = high = 0.0
        for part in parts:
            ref = part["reference"]
            if not isinstance(ref, str) or not ref or ref in references:
                raise ValueError(f"Duplicate/invalid resistor role binding: {ref}")
            references.append(ref)
            resistance = _number(part["ohms"], ref)
            if resistance <= 0:
                raise ValueError(f"{ref}: resistance must be positive")
            if "tolerance" not in part or not part.get("source"):
                omitted.append(f"{ref}: missing tolerance/source; nominal-only partial calculation")
            tolerance = _number(part.get("tolerance", 0), ref)
            if not 0 <= tolerance < 1:
                raise ValueError(f"{ref}: tolerance must be in [0, 1)")
            low += resistance * (1 - tolerance)
            nominal += resistance
            high += resistance * (1 + tolerance)
        # Every value in a sum of independent intervals is reachable. Collapsing
        # each series arm retains its exact resistance interval, not its corners.
        parameters[role] = (low, nominal, high)
        provenance[role] = parts
    for key in required_bounds:
        bound = network.get("bounds", {}).get(key)
        if bound is None:
            if key in {"supply", "reference", "common_mode"}:
                raise ValueError(f"{key}: explicit operating range required")
            omitted.append(f"{key}: missing bound; zero used only for partial illustration")
            parameters[key] = (0, 0, 0)
        else:
            parameters[key] = _range(bound, key)
            if not bound.get("source"):
                omitted.append(f"{key}: missing bound provenance")
            provenance[key] = bound
    if kind == "difference_comparator":
        if parameters["supply"][0] <= 0:
            raise ValueError("supply: positive operating range required")
        if parameters["output_low"][0] < 0 or parameters["output_high_drop"][0] < 0:
            raise ValueError("Output swing bounds must be nonnegative rail drops")
        if (
            parameters["output_low"][2] + parameters["output_high_drop"][2]
            >= parameters["supply"][0]
        ):
            raise ValueError("Output swing intervals overlap or exceed supply")
    choices = [(lo,) if lo == hi else (lo, hi) for lo, _, hi in parameters.values()]
    count = math.prod(len(values) for values in choices)
    if count > MAX_VERTICES:
        raise ValueError(f"{count} vertices exceed bounded evaluator limit {MAX_VERTICES}")
    corners = np.asarray(list(itertools.product(*choices)), dtype=float)
    sampled = dict(zip(parameters, corners.T, strict=True))
    nominal_values = {name: values[1] for name, values in parameters.items()}
    rising, falling = _thresholds(kind, sampled)
    nominal_rising, nominal_falling = _thresholds(kind, nominal_values)
    values = {"rising": rising, "falling": falling, "hysteresis": rising - falling}
    nominal_results = {
        "rising": nominal_rising,
        "falling": nominal_falling,
        "hysteresis": nominal_rising - nominal_falling,
    }
    findings: list[str] = []
    ranges: dict[str, Any] = {}
    requirements = network.get("requirements", {})
    if not requirements:
        omitted.append("No reviewed threshold requirements supplied")
    if set(requirements) - values.keys():
        raise ValueError("Unknown threshold requirement")
    for key, array in values.items():
        if not np.isfinite(array).all():
            raise ValueError("Nonfinite threshold result")
        low_index, high_index = int(np.argmin(array)), int(np.argmax(array))
        low, high = float(array[low_index]), float(array[high_index])
        ranges[key] = {
            "nominal_v": float(nominal_results[key]),
            "sample_min_v": low,
            "sample_max_v": high,
            "min_corner": {name: float(sampled[name][low_index]) for name in parameters},
            "max_corner": {name: float(sampled[name][high_index]) for name in parameters},
        }
        limits = requirements.get(key, {})
        if set(limits) - {"min", "max", "source"}:
            raise ValueError(f"{key}: unknown requirement field")
        if limits and not limits.get("source"):
            omitted.append(f"{key}: requirement provenance absent")
        if "min" in limits and low < _number(limits["min"], key) - 1e-9:
            findings.append(f"{key}: sampled {low:.8g} V below required {limits['min']} V")
        if "max" in limits and high > _number(limits["max"], key) + 1e-9:
            findings.append(f"{key}: sampled {high:.8g} V above required {limits['max']} V")
    return {
        "id": network["id"],
        "source_binding": "unbound_math_model",
        "conditions": conditions,
        "kind": kind,
        "status": "failed" if findings else "incomplete" if omitted else "sampled",
        "coverage": "incomplete" if omitted else "declared_terms_only",
        "bound_method": "vertex_sampling_not_a_proven_enclosure",
        "release_eligible": False,
        "vertices": count,
        "thresholds": ranges,
        "references": references,
        "devices": network.get("devices", {}),
        "requirements": requirements,
        "provenance": provenance,
        "findings": findings,
        "omitted_terms": omitted,
        "assumptions": [
            "Reviewed ideal linear topology; no automatic topology inference",
            "Shared supply and buffered midpoint are evaluated once per corner",
            "Common mode is (Vpositive + Vnegative)/2; differential is Vpositive - Vnegative",
            "Independent component intervals; vertices need not be physically reachable",
            "No proof of interior extrema, semiconductor operating region, dynamics or SOA",
            *network.get("assumptions", []),
        ],
    }


def analyze_thresholds(schematic: str | Path, policy_path: str | Path) -> dict[str, Any]:
    """Bind a reviewed flat-schematic sidecar to values, MPNs and connected pins."""
    from kicad_tools.operations.netlist import build_netlist_from_schematic
    from kicad_tools.schema.schematic import Schematic
    from kicad_tools.spec.units import parse_unit_value

    path, sidecar = Path(schematic), Path(policy_path)
    source = path.read_bytes()
    policy_bytes = sidecar.read_bytes()
    policy = json.loads(policy_bytes)
    if not isinstance(policy, dict):
        raise ValueError("Policy must be an object")
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "schematic_sha256": hashlib.sha256(source).hexdigest(),
        "policy_sha256": hashlib.sha256(policy_bytes).hexdigest(),
        "status": "incomplete",
        "release_eligible": False,
        "networks": [],
        "issues": [],
    }
    issues = evidence["issues"]
    if policy.get("schema_version") != 1:
        issues.append("Unsupported policy schema")
        return evidence
    if policy.get("schematic_sha256") != evidence["schematic_sha256"]:
        issues.append("Stale schematic binding: topology/value/MPN source hash changed")
        return evidence
    sch = Schematic.load(path)
    if sch.sheets:
        issues.append("Hierarchical schematic binding is not supported; no partial hierarchy pass")
        return evidence
    symbols: dict[str, Any] = {}
    for symbol in sch.symbols:
        if symbol.reference in symbols:
            issues.append(f"Duplicate reference: {symbol.reference}")
        symbols[symbol.reference] = symbol
    netlist = build_netlist_from_schematic(path)
    pins: dict[tuple[str, str], set[str]] = {}
    for net in netlist.nets:
        for node in net.nodes:
            pins.setdefault((node.reference, node.pin), set()).add(net.name)
    bindings = policy.get("bindings", {})
    networks = policy.get("networks", [])
    if not networks:
        issues.append("No networks declared")
    needed: set[str] = set()
    resistor_values: dict[str, float] = {}
    for network in networks:
        for parts in network.get("resistors", {}).values():
            for part in parts:
                needed.add(part["reference"])
                resistor_values[part["reference"]] = part["ohms"]
        devices = network.get("devices", {})
        required_devices = (
            {"comparator"}
            if network.get("kind") == "divider"
            else {"amplifier", "buffer", "comparator"}
        )
        if not required_devices <= devices.keys():
            issues.append(f"{network.get('id')}: missing active-device role bindings")
        needed.update(devices.values())
    for ref in sorted(needed):
        binding, bound_symbol = bindings.get(ref), symbols.get(ref)
        if not binding or bound_symbol is None:
            issues.append(f"{ref}: missing symbol/binding")
            continue
        if not binding.get("pins"):
            issues.append(f"{ref}: pin/net bindings absent")
        for pin, expected_net in binding.get("pins", {}).items():
            if pins.get((ref, str(pin))) != {expected_net}:
                issues.append(f"{ref}.{pin}: unbound/changed/ambiguous net {expected_net}")
        if binding.get("value") != bound_symbol.value:
            issues.append(f"{ref}: value binding mismatch")
        actual_mpn = bound_symbol.get_property("MPN") or bound_symbol.get_property(
            "Mfr_Part_Number"
        )
        if not binding.get("mpn") or binding["mpn"] != actual_mpn:
            issues.append(f"{ref}: exact MPN binding absent or changed")
        if ref in resistor_values:
            try:
                actual = parse_unit_value(bound_symbol.value)
                if actual.unit not in {"", "Ω"} or actual.value != resistor_values[ref]:
                    issues.append(f"{ref}: modeled resistance differs from schematic")
            except ValueError:
                issues.append(f"{ref}: cannot resolve schematic resistance")
    if issues:
        return evidence
    for network in networks:
        try:
            evidence["networks"].append(evaluate_network(network))
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(f"{network.get('id', '<unnamed>')}: {exc}")
    # A mutation during connectivity extraction invalidates the evidence too.
    if path.read_bytes() != source:
        issues.append("Schematic changed while calculating evidence")
    evidence["bindings"] = bindings
    for row in evidence["networks"]:
        row["source_binding"] = "verified" if not issues else "incomplete"
    evidence["status"] = (
        "failed"
        if any(row["status"] == "failed" for row in evidence["networks"])
        else "incomplete"
        if issues or any(row["status"] == "incomplete" for row in evidence["networks"])
        else "sampled"
    )
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("schematic", type=Path)
    parser.add_argument("policy", type=Path)
    args = parser.parse_args(argv)
    try:
        report = analyze_thresholds(args.schematic, args.policy)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        report = {"status": "incomplete", "release_eligible": False, "issues": [str(exc)]}
    print(json.dumps(report, indent=2, allow_nan=False))
    # Sampling can falsify a requirement, but cannot certify a release bound.
    return 1 if report["status"] == "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())

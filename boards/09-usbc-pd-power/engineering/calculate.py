#!/usr/bin/env python3
"""Reproducible component/tolerance screens, not measured performance."""

import argparse
import json
from pathlib import Path


def calculate(part_values=None):
    # Fail closed when component edits invalidate the assumptions below.
    if part_values is None:
        circuit = json.loads((Path(__file__).parents[1] / "output/circuit.json").read_text())
        part_values = {p["ref"]: p["value"] for p in circuit["parts"]}
    expected = {
        "R5": "680k",
        "R6": "100k",
        "R7": "100k 0.1%",
        "R8": "13.3k 0.1%",
        "RSH1": "10m 1% 1W",
        "L1": "10uH",
        "C5": "10u 50V",
        "C8": "22u 25V",
        "C9": "22u 25V",
    }
    for ref, value in expected.items():
        if part_values.get(ref) != value:
            raise ValueError(f"{ref}: calculation requires {value}; review changed circuit")
    rtop, rbottom, rtol = 100_000, 13_300, 0.001
    vref_min, vref_typ, vref_max = 0.581, 0.596, 0.611
    shunt, shunt_tol, iout = 0.01, 0.01, 3.0
    nominal = vref_typ * (1 + rtop / rbottom)
    low = vref_min * (1 + rtop * (1 - rtol) / (rbottom * (1 + rtol)))
    high = vref_max * (1 + rtop * (1 + rtol) / (rbottom * (1 - rtol)))
    ripple = (high * (21 - high) / 21) / (8e-6 * 290e3)
    peak = iout + ripple / 2
    rms = (iout**2 + ripple**2 / 12) ** 0.5
    upper, lower = 680_000, 100_000
    start = 1.23 * (1 + upper / lower) - 0.7e-6 * upper
    stop = 1.16 * (1 + upper / lower) - 2.25e-6 * upper
    result = {
        "component_value_binding": expected,
        "scope": "Analytical screening; thermal, transient, EMI and USB interoperability tests pending.",
        "sources": {
            "buck": "https://www.ti.com/lit/ds/symlink/tps54302.pdf",
            "inductor": "https://www.bourns.com/docs/product-datasheets/srp7050ta.pdf",
            "shunt": "https://www.vishay.com/docs/30108/wsk2512.pdf",
            "monitor": "https://www.ti.com/lit/ds/symlink/ina226.pdf",
        },
        "output": {
            "target_current_A": iout,
            "nominal_pre_shunt_V": nominal,
            "dc_component_tolerance_min_at_3A_V": low - iout * shunt * (1 + shunt_tol),
            "dc_component_tolerance_max_no_load_V": high,
            "excluded_from_bounds": [
                "load/line regulation",
                "ripple",
                "transients",
                "PCB path drop",
                "resistor TCR",
            ],
        },
        "inductor": {
            "L_nominal_uH": 10,
            "L_min_uH": 8,
            "fsw_min_Hz": 290000,
            "vin_max_V": 21,
            "ripple_pp_A": ripple,
            "peak_A": peak,
            "rms_A": rms,
            "minimum_buck_HS_limit_A": 4,
            "inductor_Isat_A": 7.5,
            "inductor_Irms_rating_A": 4,
            "DCR_max_25C_ohm": 0.069,
            "winding_loss_25C_W": rms * rms * 0.069,
            "note": "Isat and Irms use manufacturer 25C test definitions; not a guaranteed hot-board limit.",
        },
        "shunt": {
            "ohm": shunt,
            "max_drop_3A_V": iout * shunt * (1 + shunt_tol),
            "max_dissipation_3A_W": iout * iout * shunt * (1 + shunt_tol),
            "rating_at_70C_W": 1,
        },
        "enable": {
            "upper_ohm": upper,
            "lower_ohm": lower,
            "typical_start_V": start,
            "typical_stop_V": stop,
            "note": "Typical internal EN currents; measure thresholds. 5V input is below the intended operating window.",
        },
        "pd_budget": {
            "assumed_min_efficiency": 0.85,
            "logic_reserve_W": 0.1,
            "15V_1p5A_available_at_minus5pct_W": 15 * 0.95 * 1.5,
            "20V_1A_available_at_minus5pct_W": 20 * 0.95,
            "required_input_W": high * iout / 0.85 + 0.1,
            "note": "Confirm actual sink PDO/RDO and charger capability. Efficiency is a design budget, not measured.",
        },
        "monitor": {
            "current_lsb_A": 0.0002,
            "calibration": 2560,
            "power_lsb_W": 0.005,
            "shunt_lsb_V": 2.5e-6,
            "bus_lsb_V": 0.00125,
        },
        "bench_limits": {
            "initial_load_A": 0.1,
            "ambient_C": [20, 30],
            "target_load_A": 3,
            "status": "not bench validated",
        },
    }
    result["screen_passed"] = all(
        [
            low - iout * shunt * (1 + shunt_tol) >= 4.75,
            high <= 5.25,
            peak < 4,
            rms < 4,
            high * iout / 0.85 + 0.1 < 19,
        ]
    )
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "output", type=Path, nargs="?", default=Path(__file__).with_name("calculations.json")
    )
    r = calculate()
    p.parse_args().output.write_text(json.dumps(r, indent=2) + "\n")
    print(json.dumps(r, indent=2))
    raise SystemExit(0 if r["screen_passed"] else 1)

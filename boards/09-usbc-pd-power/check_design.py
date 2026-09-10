#!/usr/bin/env python3
"""Check a generated development circuit. Success is not manufacturing readiness."""

import argparse
import hashlib
import json
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from engineering.calculate import calculate

from kicad_tools.lvs.board_lvs import compare_netlists

ROOT = Path(__file__).resolve().parent


def check(output):
    schematic = output / "usbc_pd_power.kicad_sch"
    pcb = output / "usbc_pd_power.kicad_pcb"
    erc_path = output / "native-erc.json"
    erc_path.unlink(missing_ok=True)
    erc = subprocess.run(
        [
            "kicad-cli",
            "sch",
            "erc",
            str(schematic),
            "--format",
            "json",
            "--output",
            str(erc_path),
            "--exit-code-violations",
        ],
        check=False,
    )
    labels = compare_netlists(schematic, pcb)
    (output / "label-lvs.json").write_text(json.dumps(asdict(labels), indent=2) + "\n")
    circuit = json.loads((output / "circuit.json").read_text())
    calculations = calculate({p["ref"]: p["value"] for p in circuit["parts"]})
    (output / "calculations.json").write_text(json.dumps(calculations, indent=2) + "\n")
    report = {
        "scope": "Development schematic/placement checkpoint; no manufacturing release",
        "native_erc_clean": erc.returncode == 0 and erc_path.exists(),
        "label_lvs_clean": labels.clean,
        "analytical_screen_passed": calculations["screen_passed"],
        "manufacturing_ready": False,
        "hardware_tested": False,
        "unselected_mpn_refs": [p["ref"] for p in circuit["parts"] if not p["mpn"]],
        "pending": [
            "Complete routing and ground planes; native DRC and copper LVS",
            "Power-path, Kelvin, switch-loop, thermal and capacitor bias review",
            "Complete procurement and assembly-library package verification",
            "PD configuration readback and attach/inrush/unsupported-source tests",
            "Manufacturing export and independent refill identity",
        ],
        "sha256": {
            str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [
                schematic,
                pcb,
                output / "circuit.json",
                output / "net_class_map.json",
                pcb.with_suffix(".kicad_pro"),
                output / "board09.kicad_sym",
                output / "board09.pretty/SRP7050TA.kicad_mod",
            ]
        },
    }
    (output / "development-check.json").write_text(json.dumps(report, indent=2) + "\n")
    if output.resolve() == (ROOT / "output").resolve():
        inputs = {"output/" + name: digest for name, digest in report["sha256"].items()}
        for name in [
            "generate_design.py",
            "check_design.py",
            "procurement.json",
            "engineering/calculate.py",
            "output/development-check.json",
            "output/native-erc.json",
            "output/label-lvs.json",
            "output/calculations.json",
        ]:
            inputs[name] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        readiness = {
            "schema_version": 1,
            "mode": "assembly",
            "status": "blocked",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "inputs": inputs,
            "blockers": report["pending"],
            "checks": [
                {"name": key, "status": "passed" if report[key] else "failed"}
                for key in ["native_erc_clean", "label_lvs_clean", "analytical_screen_passed"]
            ]
            + [{"name": "manufacturing_release", "status": "not_run"}],
        }
        (output / "readiness.json").write_text(json.dumps(readiness, indent=2) + "\n")
    return all(
        report[key] for key in ["native_erc_clean", "label_lvs_clean", "analytical_screen_passed"]
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?", default=ROOT / "output")
    raise SystemExit(0 if check(parser.parse_args().output) else 1)

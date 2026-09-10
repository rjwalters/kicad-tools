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
from kicad_tools.schema.pcb import PCB

ROOT = Path(__file__).resolve().parent
CRITICAL_NETS = {
    "BOOT",
    "BOOT_CAP",
    "BUCK_EN",
    "FB",
    "SW",
    "VOUT_PRE",
    "+5V_OUT",
    "LED_A",
    "VBUS_FUSED",
    "VIN",
    "PMOS_SOURCE",
    "PMOS_GATE",
    "KELVIN_P",
    "KELVIN_N",
    "SENSE_P",
    "SENSE_N",
}


def critical_route_opens(pcb_path, drc_report):
    """Resolve native open findings by saved identity, not localized text."""
    board = PCB.load(pcb_path)
    items = [p for f in board.footprints for p in f.pads] + board.segments + board.vias + board.zones
    by_uuid = {item.uuid: item.net_name for item in items}
    present_nets = {p.net_name for f in board.footprints for p in f.pads}
    if not present_nets >= CRITICAL_NETS:
        raise ValueError(f"Missing critical nets: {CRITICAL_NETS - present_nets}")
    open_nets = set()
    for finding in drc_report["unconnected_items"]:
        for item in finding["items"]:
            identity = item["uuid"]
            if identity not in by_uuid:
                raise ValueError(f"Native DRC item has no saved PCB identity: {identity}")
            open_nets.add(by_uuid[identity])
    return sorted(CRITICAL_NETS & open_nets)


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
    drc_path = output / "placement-drc.json"
    drc_path.unlink(missing_ok=True)
    drc = subprocess.run(
        ["kicad-cli", "pcb", "drc", str(pcb), "--format", "json", "--output", str(drc_path)],
        check=False,
    )
    drc_report = json.loads(drc_path.read_text()) if drc_path.exists() else None
    critical_opens = critical_route_opens(pcb, drc_report) if drc_report else sorted(CRITICAL_NETS)
    labels = compare_netlists(schematic, pcb)
    (output / "label-lvs.json").write_text(json.dumps(asdict(labels), indent=2) + "\n")
    circuit = json.loads((output / "circuit.json").read_text())
    calculations = calculate({p["ref"]: p["value"] for p in circuit["parts"]})
    (output / "calculations.json").write_text(json.dumps(calculations, indent=2) + "\n")
    report = {
        "scope": "Development schematic/placement checkpoint; no manufacturing release",
        "native_erc_clean": erc.returncode == 0 and erc_path.exists(),
        "native_drc_geometry_clean": (
            drc.returncode == 0 and drc_report is not None and not drc_report["violations"]
        ),
        "unconnected_items": len(drc_report["unconnected_items"]) if drc_report else None,
        "critical_routes_connected": drc.returncode == 0 and not critical_opens,
        "critical_net_opens": critical_opens,
        "completed_critical_nets": sorted(CRITICAL_NETS - set(critical_opens)),
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
            "output/placement-drc.json",
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
                for key in [
                    "native_erc_clean",
                    "native_drc_geometry_clean",
                    "critical_routes_connected",
                    "label_lvs_clean",
                    "analytical_screen_passed",
                ]
            ]
            + [{"name": "manufacturing_release", "status": "not_run"}],
        }
        (output / "readiness.json").write_text(json.dumps(readiness, indent=2) + "\n")
    return all(
        report[key]
        for key in [
            "native_erc_clean",
            "native_drc_geometry_clean",
            "critical_routes_connected",
            "label_lvs_clean",
            "analytical_screen_passed",
        ]
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?", default=ROOT / "output")
    raise SystemExit(0 if check(parser.parse_args().output) else 1)

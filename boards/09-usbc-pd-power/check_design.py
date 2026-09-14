#!/usr/bin/env python3
"""Check a generated development circuit. Success is not manufacturing readiness."""

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from engineering.calculate import calculate

from kicad_tools.lvs.board_lvs import compare_netlists
from kicad_tools.lvs.copper_lvs import compare_copper_netlist
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
    "GND",
    "+3V3",
    "VBUS_RAW",
    "CC1",
    "CC2",
    "VBUS_SENSE",
    "VREG_1V2",
    "VREG_2V7",
    "DISCHARGE",
    "SINK_GATE",
    "PD_OK2",
    "PD_OK3",
    "SDA",
    "SCL",
    "PD_ALERT",
    "MON_ALERT",
}


# Fixed convention (issue #5170): a board opts into the component-stress gate
# by shipping this file alongside its schematic. No board ships one yet, so
# absence must leave every existing check untouched.
COMPONENT_STRESS_MANIFEST = ROOT / "operating_states.yaml"


def _component_stress_blocker(row: dict) -> str:
    """Render one FAIL/UNRESOLVED census row as a human-readable blocker.

    Reuses the detail already carried by :meth:`ComponentStressResult.to_dict`
    (reference/state/check/reason) rather than inventing a new format.
    """
    detail = row.get("reason")
    if not detail:
        detail = (
            f"stress={row.get('stress_v')}V rated={row.get('rated_v')}V "
            f"margin={row.get('margin_v')}V"
        )
    return (
        f"Component stress {row['status']}: {row['reference']} {row['check'].upper()} "
        f"in state '{row['state']}' ({detail})"
    )


def evaluate_component_stress(
    schematic_path: Path, manifest_path: Path = COMPONENT_STRESS_MANIFEST
) -> tuple[bool | None, list[str]]:
    """Run the MOSFET VDS/VGS stress gate when a manifest is present.

    Returns ``(component_stress_clean, blockers)``. ``component_stress_clean``
    is ``None`` when *manifest_path* does not exist -- the caller must not add
    a ``component_stress`` checks entry or any new blocker in that case, so
    boards without a manifest are completely unaffected (issue #5170).

    A manifest that exists but fails to load (``OperatingStateManifest.load``
    raises ``FileNotFoundError``/``ValueError`` on a malformed document) is
    reported as a single blocker with ``component_stress_clean=False`` rather
    than raising. ``ComponentStressAnalyzer.analyze`` itself never raises
    (advisory contract), so no further exception handling is needed around it.
    """
    if not manifest_path.exists():
        return None, []

    from kicad_tools.analysis import ComponentStressAnalyzer, OperatingStateManifest

    try:
        manifest = OperatingStateManifest.load(manifest_path)
    except (FileNotFoundError, ValueError) as exc:
        return False, [f"Component-stress manifest {manifest_path.name} could not be loaded: {exc}"]

    rows = [r.to_dict() for r in ComponentStressAnalyzer(manifest).analyze(schematic_path)]
    bad_rows = [r for r in rows if r["status"] in ("FAIL", "UNRESOLVED")]
    return not bad_rows, [_component_stress_blocker(r) for r in bad_rows]


def inner_ground_planes_valid(board):
    inner = {"In1.Cu", "In2.Cu"}
    zones = [z for z in board.zones if z.layer in inner and z.keepout is None]
    return (
        len(zones) == 2
        and {z.layer for z in zones} == inner
        and all(z.net_name == "GND" and z.filled_polygons for z in zones)
        and not any(s.layer in inner and s.net_name != "GND" for s in board.segments)
    )


def critical_route_opens(pcb_path, drc_report):
    """Resolve native open findings by saved identity, not localized text."""
    board = PCB.load(pcb_path)
    items = (
        [p for f in board.footprints for p in f.pads] + board.segments + board.vias + board.zones
    )
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
        [
            "kicad-cli",
            "pcb",
            "drc",
            str(pcb),
            "--refill-zones",
            "--save-board",
            "--format",
            "json",
            "--output",
            str(drc_path),
        ],
        check=False,
    )
    drc_report = json.loads(drc_path.read_text()) if drc_path.exists() else None
    critical_opens = critical_route_opens(pcb, drc_report) if drc_report else sorted(CRITICAL_NETS)
    labels = compare_netlists(schematic, pcb)
    (output / "label-lvs.json").write_text(json.dumps(asdict(labels), indent=2) + "\n")
    copper = compare_copper_netlist(schematic, pcb)
    (output / "copper-lvs.json").write_text(json.dumps(asdict(copper), indent=2) + "\n")
    # Record independent tool findings even while manufacturing remains blocked.
    tool_reports = {}
    for name, args in [
        ("manufacturer-check", ["check", str(pcb), "--mfr", "jlcpcb", "--format", "json"]),
        ("net-status", ["net-status", str(pcb), "--strict", "--format", "json"]),
    ]:
        result = subprocess.run(
            [sys.executable, "-m", "kicad_tools.cli", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{name} did not produce JSON: {result.stderr}") from exc
        (output / f"{name}.json").write_text(json.dumps(data, indent=2) + "\n")
        tool_reports[name] = {"returncode": result.returncode, "summary": data["summary"]}
    circuit = json.loads((output / "circuit.json").read_text())
    calculations = calculate({p["ref"]: p["value"] for p in circuit["parts"]})
    (output / "calculations.json").write_text(json.dumps(calculations, indent=2) + "\n")
    component_stress_clean, component_stress_blockers = evaluate_component_stress(
        schematic, COMPONENT_STRESS_MANIFEST
    )
    report = {
        "scope": "Routed development checkpoint; no manufacturing release",
        "native_erc_clean": erc.returncode == 0 and erc_path.exists(),
        "native_drc_geometry_clean": (
            drc.returncode == 0 and drc_report is not None and not drc_report["violations"]
        ),
        "unconnected_items": len(drc_report["unconnected_items"]) if drc_report else None,
        "all_nets_connected": drc_report is not None and not drc_report["unconnected_items"],
        "critical_routes_connected": drc.returncode == 0 and not critical_opens,
        "inner_ground_planes_valid": inner_ground_planes_valid(PCB.load(pcb)),
        "critical_net_opens": critical_opens,
        "completed_critical_nets": sorted(CRITICAL_NETS - set(critical_opens)),
        "label_lvs_clean": labels.clean,
        "copper_lvs_clean": copper.clean,
        "copper_lvs_bound_pads": copper.bound_pad_count,
        "manufacturer_rules_passed": tool_reports["manufacturer-check"]["summary"]["passed"],
        "tool_reports": tool_reports,
        "analytical_screen_passed": calculations["screen_passed"],
        "manufacturing_ready": False,
        "hardware_tested": False,
        "unselected_mpn_refs": [p["ref"] for p in circuit["parts"] if not p["mpn"]],
        "pending": [
            "Resolve manufacturer ampacity findings through branch-current and thermal review",
            "Reconcile strict GND connectivity disagreement with native KiCad (#5061)",
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
    if component_stress_clean is not None:
        report["component_stress_clean"] = component_stress_clean
        report["component_stress_blockers"] = component_stress_blockers
    (output / "development-check.json").write_text(json.dumps(report, indent=2) + "\n")
    if output.resolve() == (ROOT / "output").resolve():
        inputs = {"output/" + name: digest for name, digest in report["sha256"].items()}
        for name in [
            "generate_design.py",
            "check_design.py",
            "procurement.json",
            "project.kct",
            "engineering/calculate.py",
            "output/development-check.json",
            "output/native-erc.json",
            "output/placement-drc.json",
            "output/label-lvs.json",
            "output/copper-lvs.json",
            "output/manufacturer-check.json",
            "output/net-status.json",
            "output/calculations.json",
        ]:
            inputs[name] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        readiness_blockers = list(report["pending"])
        readiness_checks = [
            {"name": key, "status": "passed" if report[key] else "failed"}
            for key in [
                "native_erc_clean",
                "native_drc_geometry_clean",
                "critical_routes_connected",
                "inner_ground_planes_valid",
                "all_nets_connected",
                "label_lvs_clean",
                "copper_lvs_clean",
                "analytical_screen_passed",
                "manufacturer_rules_passed",
            ]
        ]
        # Only add a component_stress checks entry / new blockers when a
        # board-local operating-state manifest is present -- boards without
        # one (every board today) must see byte-identical readiness output
        # to before this check existed (issue #5170). When it IS present, its
        # bytes must be bound into inputs too, or editing/deleting it after a
        # passing report leaves every recorded hash unchanged and the site
        # readiness loader keeps presenting the stale check as verified.
        if component_stress_clean is not None:
            inputs["operating_states.yaml"] = hashlib.sha256(
                COMPONENT_STRESS_MANIFEST.read_bytes()
            ).hexdigest()
            readiness_blockers = readiness_blockers + component_stress_blockers
            readiness_checks.append(
                {
                    "name": "component_stress",
                    "status": "passed" if component_stress_clean else "failed",
                }
            )
        readiness_checks.append({"name": "manufacturing_release", "status": "not_run"})
        readiness = {
            "schema_version": 1,
            "mode": "assembly",
            "status": "blocked",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "inputs": inputs,
            "blockers": readiness_blockers,
            "checks": readiness_checks,
        }
        (output / "readiness.json").write_text(json.dumps(readiness, indent=2) + "\n")
    return all(
        report[key]
        for key in [
            "native_erc_clean",
            "native_drc_geometry_clean",
            "critical_routes_connected",
            "inner_ground_planes_valid",
            "all_nets_connected",
            "label_lvs_clean",
            "copper_lvs_clean",
            "analytical_screen_passed",
        ]
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?", default=ROOT / "output")
    raise SystemExit(0 if check(parser.parse_args().output) else 1)

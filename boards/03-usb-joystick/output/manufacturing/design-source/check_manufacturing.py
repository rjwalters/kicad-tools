#!/usr/bin/env python3
"""Run all kct checks with this board's explicitly reviewed fabrication floor.

Usage: uv run python boards/03-usb-joystick/check_manufacturing.py PCB [REPORT]
No findings are suppressed. Native project/process settings must agree first.
The report keeps normal DRC/ERC/LVS/manifest results plus override provenance.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from routing_plan import (
    PLAN,
    fingerprint,
    load_board_fabrication_overrides,
    physical_contract,
    select_fabrication_override,
    usb_geometry,
)

from kicad_tools.cli.check_cmd import (
    SubCheckResult,
    run_meta_checks,
    run_selected_checks,
    write_json_report,
)
from kicad_tools.manufacturers.fabrication_overrides import apply_fabrication_overrides
from kicad_tools.router.rules import net_class_map_from_dict
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate import DRCChecker

# Issue #5006: the board's validated, cited pad-hole-spacing floor lives in a
# ``fabrication_overrides.json`` sidecar (single source of truth), consumed
# identically by the Python checker below and by every
# native-constraint-emission call site (``routing_plan.apply_native_fab_floor``,
# ``kct check --emit-drc-constraints``, etc.) via
# ``resolve_pcb_fabrication_overrides``. ``routing_plan`` owns locating it --
# the shared three-directory probe around the PCB, falling back to this
# board's committed sidecar -- so a board generated outside
# ``boards/03-usb-joystick/output/`` resolves the identical, cited floor
# instead of dying on a missing file.
HOLE_FLOOR_FIELD = "min_hole_to_hole_mm"


def make_checker(pcb_path: Path, overrides=None) -> DRCChecker:
    """Fail closed on mismatched fabrication settings; never modify the input."""
    # ``load_board_fabrication_overrides`` fails loud (raises) on a missing or
    # invalid sidecar, matching this script's "fail closed" contract -- unlike
    # ``resolve_pcb_fabrication_overrides``, which degrades gracefully for the
    # CLI/export call sites where a missing override is an expected case.
    if overrides is None:
        overrides = load_board_fabrication_overrides(pcb_path)
    hole_floor = select_fabrication_override(overrides, HOLE_FLOOR_FIELD)
    options = json.loads((pcb_path.parent / "manufacturing-requirements.json").read_text())
    plan = json.loads(PLAN.read_text())
    expected = plan["required_factory_options"]
    if fingerprint(pcb_path) != plan["physical_sha256"]:
        raise ValueError("Physical circuit differs from reviewed routing plan")
    if options != expected:
        raise ValueError("Manufacturing options differ from reviewed routing plan")
    project = json.loads(pcb_path.with_suffix(".kicad_pro").read_text())
    if project["board"]["design_settings"]["rules"]["min_hole_to_hole"] != hole_floor.value:
        raise ValueError(f"Native pad-hole floor must equal reviewed {hole_floor.value} mm")
    physical = physical_contract(pcb_path)
    if physical["via_filling"] != "yes" or physical["via_capping"] != "yes":
        raise ValueError("Filled and capped vias are mandatory")
    pcb = PCB.load(pcb_path)
    if len(pcb.copper_layers) != 4:
        raise ValueError("Reviewed fabrication requires four copper layers")
    mapping = net_class_map_from_dict(
        json.loads((pcb_path.parent / "net_class_map.json").read_text())
    )
    if not {"USB_D+", "USB_D-", "USB_MCU_D+", "USB_MCU_D-"} <= mapping.keys():
        raise ValueError("Missing USB pair constraints")
    checker = DRCChecker(
        pcb,
        manufacturer="jlcpcb-tier1",
        layers=4,
        net_class_map=mapping,
        emit_measurements=True,
        copper_oz_outer=1.0,
        copper_oz_inner=0.0152 / 0.035,
    )
    # Issue #5006: apply this board's validated, cited pad-hole-spacing
    # override through the shared fabrication-overrides contract instead of
    # a bespoke ``dataclasses.replace`` patch -- so the Python checker and
    # every native-constraint-emission call site resolve the identical
    # floor from the identical, cited source.
    checker.design_rules = apply_fabrication_overrides(
        checker.design_rules, overrides, manufacturer_id="jlcpcb-tier1"
    )
    return checker


def check(pcb_path: Path, report: Path) -> dict:
    # Load the sidecar once and hand the same overrides to the checker and to
    # the report provenance below, so the two can never disagree.
    overrides = load_board_fabrication_overrides(pcb_path)
    override = select_fabrication_override(overrides, HOLE_FLOOR_FIELD)
    checker = make_checker(pcb_path, overrides=overrides)
    geometry = usb_geometry(pcb_path)
    schematic = pcb_path.parent / "usb_joystick.kicad_sch"
    results = run_selected_checks(checker, None, set(), sch_path=schematic, pcb_path=pcb_path)
    violations = results.violations
    errors = sum(v.is_error for v in violations)
    warnings = sum(v.is_warning for v in violations)
    status = SubCheckResult(
        "PASSED" if not errors and not warnings else "FAILED",
        f"{errors} errors, {warnings} warnings; reviewed {override.value} mm pad-hole floor",
    )
    meta = run_meta_checks(pcb_path, status, schematic=str(schematic), strict=True)
    write_json_report(violations, results, pcb_path, "jlcpcb-tier1", 4, report, meta=meta)
    data = json.loads(report.read_text())
    # Report provenance straight from the sidecar (single source of truth,
    # Issue #5006) rather than re-stating it here, so the report can never
    # drift from the override the checker actually applied above.
    data["fabrication_overrides"] = {
        HOLE_FLOOR_FIELD: override.value,
        "source": override.source,
        "reason": override.reason,
        "tracking_issue": override.tracking_issue,
        "suppressed_findings": 0,
    }
    data["usb_geometry"] = geometry
    report.write_text(json.dumps(data, indent=2) + "\n")
    return data


if __name__ == "__main__":
    pcb = Path(sys.argv[1]).resolve()
    report = Path(sys.argv[2]) if len(sys.argv) > 2 else pcb.parent / "check-report.json"
    data = check(pcb, report)
    print(json.dumps({"summary": data["summary"], "meta_checks": data["meta_checks"]}, indent=2))
    sys.exit(0 if data["meta_checks"]["overall"] == "PASSED" else 2)

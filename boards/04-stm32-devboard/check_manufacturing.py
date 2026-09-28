#!/usr/bin/env python3
"""Run all checks against board04's explicit paid mechanical-drilling process."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from manufacturing_process import OPTIONS, make_checker

from kicad_tools.cli.check_cmd import (
    SubCheckResult,
    run_meta_checks,
    run_selected_checks,
    write_json_report,
)
from kicad_tools.validate.rules.waivers import (
    apply_waivers,
    discover_waivers_sidecar,
    load_waivers,
)


def check(pcb_path, report_path):
    pcb_path, report_path = Path(pcb_path), Path(report_path)
    checker = make_checker(pcb_path)
    sch = pcb_path.parent / "stm32_devboard.kicad_sch"
    results = run_selected_checks(checker, None, set(), sch_path=sch, pcb_path=pcb_path)
    # Apply the committed .kct_waivers.json sidecar exactly as ``kct check``
    # does (post-check step).  A malformed committed sidecar is a hard error
    # here: this is a reviewed release gate, not a best-effort CLI run.
    waivers_path = discover_waivers_sidecar(pcb_path)
    if waivers_path is not None:
        apply_waivers(results, load_waivers(waivers_path))
    errors = sum(v.is_error for v in results.violations)
    warnings = sum(v.is_warning for v in results.violations)
    drc = SubCheckResult(
        "PASSED" if not errors and not warnings else "FAILED",
        f"{errors} errors, {warnings} warnings",
    )
    meta = run_meta_checks(pcb_path, drc, schematic=str(sch), strict=True)
    write_json_report(
        results.violations, results, pcb_path, "jlcpcb-tier1", 0, report_path, meta=meta
    )
    data = json.loads(report_path.read_text())
    data["fabrication_overrides"] = {
        **OPTIONS,
        "suppressed_findings": 0,
        "waived_findings": sum(v.waived for v in results.violations),
        "tracking_issue": "https://github.com/rjwalters/kicad-tools/issues/5009",
    }
    report_path.write_text(json.dumps(data, indent=2) + "\n")
    return data


if __name__ == "__main__":
    pcb = Path(sys.argv[1]).resolve()
    report = Path(sys.argv[2]) if len(sys.argv) > 2 else pcb.parent / "check-report.json"
    data = check(pcb, report)
    failed = data["meta_checks"]["overall"] != "PASSED"
    display = data if failed else {"summary": data["summary"], "meta_checks": data["meta_checks"]}
    print(json.dumps(display, indent=2))
    sys.exit(0 if data["meta_checks"]["overall"] == "PASSED" else 2)

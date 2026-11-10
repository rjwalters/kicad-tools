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
from kicad_tools.validate.known_warnings import known_warning_count, unaccepted_warnings
from kicad_tools.validate.rules.waivers import (
    apply_waivers,
    discover_waivers_sidecar,
    load_waivers,
)

#: Issue #7298 / #7295: the nine 0805s whose pad 1 the starved-thermal repair
#: used to force solid (``thermal_asymmetry``) now take a narrower spoke
#: (#7295), so board 04 has no accepted warnings left.  The mechanism stays so
#: a future reviewed exception is a pinned ``(rule, ref, pad)`` entry, not a
#: loosened gate; any warning or error fails the DRC leg.
BOARD04_KNOWN_WARNINGS: dict[tuple[str, str, str], str] = {}


def drc_verdict(violations) -> SubCheckResult:
    """The DRC meta-check leg: 0 errors and no warning outside the known list."""
    violations = list(violations)
    errors = sum(v.is_error for v in violations)
    warnings = sum(v.is_warning for v in violations)
    extra = unaccepted_warnings(violations, BOARD04_KNOWN_WARNINGS)
    detail = f"{errors} errors, {warnings} warnings"
    known = known_warning_count(violations, BOARD04_KNOWN_WARNINGS)
    if known:
        detail += f" ({known} known, #7295)"
    if extra:
        detail += f"; unaccepted: {'; '.join(extra)}"
    return SubCheckResult("PASSED" if not errors and not extra else "FAILED", detail)


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
    drc = drc_verdict(results.violations)
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

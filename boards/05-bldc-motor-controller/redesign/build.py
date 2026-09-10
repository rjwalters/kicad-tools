"""Rebuild revision B from source and fingerprinted copper, then run real gates."""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from hardware import ROOT, generate
from routing import apply_routing


def build(output):
    output = Path(output).resolve()
    generate(output)
    # A local copy lets procurement/export discover exact identities in scratch.
    (output / "project.kct").write_text((ROOT / "project.kct").read_text().replace("output/", ""))
    routed = output / "bldc_controller_routed.kicad_pcb"
    shutil.copy2(output / "bldc_controller.kicad_pcb", routed)
    shutil.copy2(output / "bldc_controller.kicad_pro", routed.with_suffix(".kicad_pro"))
    fingerprint = apply_routing(routed)
    evidence = output / "readiness"
    evidence.mkdir(exist_ok=True)
    cli = [sys.executable, "-m", "kicad_tools.cli"]
    # First check emits current manufacturer DRU. Unfilled zones are not a pass.
    subprocess.run(
        cli
        + [
            "check",
            str(routed),
            "--mfr",
            "jlcpcb",
            "--emit-dru",
            "--format",
            "json",
            "--output",
            str(evidence / "before-refill.json"),
        ],
        stdout=subprocess.DEVNULL,
        check=False,
    )
    subprocess.run(
        [
            "kicad-cli",
            "pcb",
            "drc",
            str(routed),
            "--refill-zones",
            "--save-board",
            "--format",
            "json",
            "--output",
            str(evidence / "native-drc.json"),
        ],
        check=True,
    )
    native = json.loads((evidence / "native-drc.json").read_text())
    if native["violations"] or native["unconnected_items"]:
        raise RuntimeError("Fresh native refill/DRC failed; do not manufacture")
    subprocess.run(
        [
            "kicad-cli",
            "sch",
            "erc",
            str(output / "bldc_controller.kicad_sch"),
            "--format",
            "json",
            "--output",
            str(evidence / "native-erc.json"),
        ],
        check=True,
    )
    erc = json.loads((evidence / "native-erc.json").read_text())
    if any(sheet["violations"] for sheet in erc["sheets"]):
        raise RuntimeError("Fresh native schematic ERC failed")
    from kicad_tools.lvs import write_lvs_report

    write_lvs_report(output / "bldc_controller.kicad_sch", routed, output, require_clean=True)
    subprocess.run(
        cli
        + [
            "check",
            str(routed),
            "--mfr",
            "jlcpcb",
            "--format",
            "json",
            "--output",
            str(evidence / "kct-check.json"),
        ],
        stdout=subprocess.DEVNULL,
        check=False,
    )
    check = json.loads((evidence / "kct-check.json").read_text())
    if check["summary"]["errors"] or check["summary"]["warnings"]:
        raise RuntimeError("Manufacturer check contains errors or warnings")
    if any(check["meta_checks"][gate]["status"] != "PASSED" for gate in ("drc", "erc", "lvs")):
        raise RuntimeError("Electrical/manufacturer meta-check failed")
    print(f"Revision B rebuilt and verified; pad/placement fingerprint {fingerprint}")
    return routed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", type=Path, default=ROOT.parent / "output")
    args = parser.parse_args()
    from package import export_package

    # Failed regeneration never replaces the last reviewed manufacturing output.
    with tempfile.TemporaryDirectory(prefix="bldc-rebuild-") as staging:
        stage = Path(staging)
        build(stage)
        export_package(stage)
        if args.output.exists():
            backup = Path(tempfile.mkdtemp(prefix="bldc-previous-output-")) / "output"
            shutil.copytree(args.output, backup)
            print(f"Previous output preserved at {backup}")
            shutil.rmtree(args.output)
        shutil.copytree(stage, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

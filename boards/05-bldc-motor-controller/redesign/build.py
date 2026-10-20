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

from kicad_tools.validate.known_warnings import unaccepted_report_warnings

#: Issue #7298: no warning is accepted.  #7296 (the router refuses pad-edge
#: vias) removed the three ``via_at_pad_edge`` findings (R11-1, R9-1, D1-1)
#: this board used to pin, so any warning now fails; errors never pass.
BOARD05_KNOWN_WARNINGS: dict[tuple[str, str, str], str] = {}


def manufacturer_check_failures(check):
    """Why a ``kct check`` report fails this build's gate (empty when it passes)."""
    failures = []
    if check["summary"]["errors"]:
        failures.append(f"{check['summary']['errors']} error(s)")
    failures += unaccepted_report_warnings(check, BOARD05_KNOWN_WARNINGS)
    return failures


#: The router net classes the committed routed sidecars declare (#6390) and
#: whose copper #6395 repaired.  ``Reviewed clearance - <class>`` rules for
#: these must survive every regeneration (#7391).
ROUTER_NETCLASSES = (
    "Audio",
    "Clock",
    "Debug",
    "Differential",
    "Digital",
    "HighCurrentSignal",
    "Power",
)


def declare_router_netclasses(routed):
    """Declare the router's net classes in the routed sidecars (#6390, #7391).

    ``routing.json`` was routed by ``kct route`` at the router's per-class
    clearances (Power/Clock/HighCurrentSignal 0.2 mm, Audio/Debug/
    Differential/Digital 0.15 mm), and #6390 declared those classes in the
    committed ``.kicad_pro``/``.kicad_dru`` so that kicad-cli and ``kct
    check`` hold the copper to them (#6395 repaired the copper that sat
    below).  The authored project from ``hardware.generate`` declares only
    ``Default``, so without this step a regeneration silently dropped the
    seven ``Reviewed clearance - <class>`` rules, loosening those nets to the
    0.1016 mm fab floor.

    The map is the router's effective one (``DEFAULT_NET_CLASS_MAP`` plus
    its name classifier), exactly as ``scripts/boards/
    declare_router_netclasses_6390.py`` derived the committed sidecars.  The
    sidecar writer keeps stricter authored minima (``min_track_width``
    0.15 mm, the routed copper's narrowest track).
    """
    from kicad_tools.cli.route_cmd import _write_drc_constraint_sidecars
    from kicad_tools.manufacturers.project_generator import board_net_names
    from kicad_tools.router.net_class import classify_and_apply_rules
    from kicad_tools.router.rules import DEFAULT_NET_CLASS_MAP

    nets = sorted(name for name in (board_net_names(routed) or ()) if name)
    effective = dict(DEFAULT_NET_CLASS_MAP)
    for name, routing in classify_and_apply_rules(dict(enumerate(nets, 1))).items():
        effective.setdefault(name, routing)
    _write_drc_constraint_sidecars(routed, "jlcpcb", 4, quiet=True, net_class_map=effective)
    project = json.loads(routed.with_suffix(".kicad_pro").read_text())
    declared = project.get("meta", {}).get("kct_router_netclasses", {}).get("classes", [])
    if tuple(sorted(declared)) != ROUTER_NETCLASSES:
        raise RuntimeError(f"Router net classes not declared in the routed project: {declared}")


def build(output):
    output = Path(output).resolve()
    generate(output)
    # A local copy lets procurement/export discover exact identities in scratch.
    (output / "project.kct").write_text((ROOT / "project.kct").read_text().replace("output/", ""))
    # Published beside the PCB so `kct check` auto-loads the declared ampacity targets.
    shutil.copy2(ROOT / "net_class_map.json", output / "net_class_map.json")
    routed = output / "bldc_controller_routed.kicad_pcb"
    shutil.copy2(output / "bldc_controller.kicad_pcb", routed)
    shutil.copy2(output / "bldc_controller.kicad_pro", routed.with_suffix(".kicad_pro"))
    fingerprint = apply_routing(routed)
    declare_router_netclasses(routed)
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
    # The shared stage proves every plane net on actual refilled copper.
    # A failed or dangling stitch never replaces the staged input, and its
    # complete candidate/evidence directory survives outside recipe staging.
    from kicad_tools.stitching import complete_power_connections

    stitch_evidence = Path(tempfile.mkdtemp(prefix="bldc-power-stitch-")) / "evidence"
    complete_power_connections(routed, evidence_dir=stitch_evidence)
    shutil.copytree(stitch_evidence, evidence / "stitch")
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
    from kicad_tools.cli.kicad_toolchain import require_library_environment

    # Issue #6962: a library-table finding describes the environment, not
    # the board, so it must never become evidence.
    require_library_environment(native)
    # Only "error"-severity findings are a fab-stop. kicad-cli's default JSON
    # includes warnings in `violations` too (e.g. the JLCPCB "Silk to Pad"
    # floor is a legibility/DFM limit KiCad itself classifies as a warning,
    # per dru_generator.py's rationale for leaving it unforced) -- gating on
    # any violation regardless of severity turned a cosmetic advisory into
    # "do not manufacture".
    blocking = [v for v in native["violations"] if v.get("severity") == "error"]
    if blocking or native["unconnected_items"]:
        # main() removes failed staging directories; keep the actual native
        # findings in the CI log so a missing library or copper fault is clear.
        print(json.dumps(native, indent=2), file=sys.stderr)
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
        print(json.dumps(erc, indent=2), file=sys.stderr)
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
    failures = manufacturer_check_failures(check)
    if failures:
        raise RuntimeError(
            "Manufacturer check contains errors or unaccepted warnings: " + "; ".join(failures)
        )
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
    from kicad_tools.cli.kicad_toolchain import run_on_evidence_toolchain

    # Issue #6962: every bare ``kicad-cli`` this producer and package.py run
    # resolves to the pinned evidence toolchain (KCT_KICAD_DOCKER=1 routes it
    # through CI's image); any other release is refused before anything is
    # written.
    raise SystemExit(run_on_evidence_toolchain(main, [ROOT.parents[2]]))

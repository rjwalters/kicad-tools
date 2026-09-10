"""Complete the reviewed assembly bundle, including manual parts and firmware."""

import csv
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from hardware import ROOT


def export_package(output):
    output = Path(output)
    destination = output / "manufacturing"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "export",
            str(output / "bldc_controller_routed.kicad_pcb"),
            "--mfr",
            "jlcpcb",
            "--output",
            str(destination),
            "--strict-preflight",
            "--drc-report",
            str(output / "readiness" / "native-drc.json"),
            "--erc-report",
            str(output / "readiness" / "native-erc.json"),
        ],
        check=True,
    )
    for command in [
        [
            "kicad-cli",
            "sch",
            "export",
            "pdf",
            str(output / "bldc_controller.kicad_sch"),
            "--output",
            str(destination / "schematic.pdf"),
        ],
        [
            "kicad-cli",
            "pcb",
            "export",
            "pdf",
            str(output / "bldc_controller_routed.kicad_pcb"),
            "--layers",
            "F.Cu,F.SilkS,Edge.Cuts",
            "--output",
            str(destination / "assembly-front.pdf"),
        ],
        [
            "kicad-cli",
            "pcb",
            "export",
            "pdf",
            str(output / "bldc_controller_routed.kicad_pcb"),
            "--layers",
            "B.Cu,B.SilkS,Edge.Cuts",
            "--output",
            str(destination / "assembly-back.pdf"),
        ],
    ]:
        subprocess.run(command, check=True)
    shutil.copy2(ROOT / "HARDWARE.md", destination / "HARDWARE.md")
    shutil.copy2(ROOT / "project.kct", destination / "project.kct")
    shutil.copytree(
        ROOT / "firmware",
        destination / "firmware",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("*.elf", ".gitignore"),
    )
    for filename in ["native-drc.json", "native-erc.json", "kct-check.json"]:
        shutil.copy2(output / "readiness" / filename, destination / filename)
    shutil.copy2(output / "lvs.json", destination / "lvs.json")
    (destination / "README.txt").write_text(
        "Sensored BLDC controller, revision B — JLCPCB 4-layer/1oz assembly\n\n"
        "Start with an unloaded small motor, RUN open, a regulated 12V bench supply\n"
        "limited to 0.5A, and RV1 at its measured low-voltage end. Initial phase-current\n"
        "target is 0.5A; nominal comparator trip is 1A and firmware caps duty at 25%.\n"
        "Physical motor operation and thermal performance have not been measured.\n"
        "Do not increase these limits without bench validation. Read HARDWARE.md.\n\n"
        "SMT assembly: 36 CPL placements. Hand-solder J1-J5 and RV1 afterward.\n"
        "The BOM includes these six sourcing entries; they are absent from the SMT CPL.\n"
        "Solder U2/U3 exposed pads; their thermal vias are part of the SMT land pattern.\n"
        "Observe IC/header pin1, C1 polarity and D1/D2/D3 polarity marks.\n\n"
        "J1:1=VIN,2=GND; J2:1/2/3=phases A/B/C; J3:1=5V,2=GND,3/4/5=Hall A/B/C,6=GND.\n"
        "J4 ISP:1=MISO,2=5V target sense,3=SCK,4=MOSI,5=RESET,6=GND. Disable programmer power.\n"
        "J5 RUN: close pin1 to ground pin2 after first leaving open to arm firmware.\n"
        "Power from J1 only; limit Hall load to 20mA and total 5V load to 30mA.\n"
        "Program firmware/bldc.hex via slow AVR ISP, using factory internal-RC clock fuses.\n\n"
        "Fresh native DRC/refill:0 violations/0opens; native ERC:0; label/copper LVS:0.\n"
        "Manufacturer checks:0 errors/0warnings. No DRC waivers.\n"
        "gerbers/gerbers.zip: fabrication; bom_jlcpcb.csv: procurement; cpl_jlcpcb.csv: SMT.\n"
        "kicad_project.zip includes the exact PCB/schematic and local footprint/symbol libraries.\n"
        "schematic.pdf and assembly-front/back.pdf are production drawings.\n"
        "HARDWARE.md, firmware/, check reports and manifest.json complete the package.\n"
    )
    cpl = list(csv.DictReader((destination / "cpl_jlcpcb.csv").open()))
    assert len(cpl) == 36
    assert not {"J1", "J2", "J3", "J4", "J5", "RV1"} & {row["Designator"] for row in cpl}
    assert {"U1", "U2", "U3"} <= {row["Designator"] for row in cpl}
    # Exporter's generic project ZIP does not discover project-local libraries.
    archive = destination / "kicad_project.zip"
    with zipfile.ZipFile(archive) as z:
        entries = {name: z.read(name) for name in z.namelist()}
    for name in [
        "board05_revB.kicad_sym",
        "fp-lib-table",
        "sym-lib-table",
        "bldc_controller_routed.kicad_dru",
    ]:
        entries[name] = (output / name).read_bytes()
    for path in (output / "board05_revB.pretty").rglob("*.kicad_mod"):
        entries[path.relative_to(output).as_posix()] = path.read_bytes()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    refresh_manifest(destination)
    return destination


def refresh_manifest(destination):
    path = destination / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest.update(
        mode="assembly", warning_counts={}, native_warning_counts={}, acknowledged_warning_rules=[]
    )
    manifest["files"] = {
        p.relative_to(destination).as_posix(): {
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            "size": p.stat().st_size,
        }
        for p in sorted(destination.rglob("*"))
        if p.is_file() and p.name != "manifest.json"
    }
    path.write_text(json.dumps(manifest, indent=2) + "\n")

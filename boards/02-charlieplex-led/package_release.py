#!/usr/bin/env python3
"""Rebuild board 02's shipped manufacturing package through ``kct readiness``.

Usage::

    uv run python boards/02-charlieplex-led/package_release.py

Run it after ``output/*.kicad_sch`` / ``output/*.kicad_pcb`` change.  It runs
the generic ``kct readiness --generate`` producer (see
``boards/release_package.py``) and adds what this board's package has shipped
since the demo-board release (d95b6eff / df2134e5), each re-derived or
re-validated by this run:

* Board checks (also written into ``readiness.json``):

  - ``firmware`` -- ``firmware/main.c``, ``Makefile`` and the programmed
    image ``charlieplex.hex`` must be byte-identical to the compiled audit
    (AVR GCC 7.3.0: 214 flash bytes, 22 RAM bytes); the Intel HEX is
    re-parsed (record checksums) and its flash size re-measured.  When
    ``avr-gcc`` is installed the image is also rebuilt and compared.
  - ``smt_drill_clearance`` -- the Issue #5012 drill-vs-SMT-land rule
    (``ViaInPadRule``, JLCPCB 2-layer rules) re-run on the saved board, and
    every reviewed relocation in ``output/readiness/drill-relocations.json``
    re-located on its net.
  - ``native_erc`` -- fresh ``kicad-cli sch erc``; zero errors/warnings.

* Package files: ``kct-check.json``/``check-report.json``, ``native-drc.json``,
  ``native-erc.json`` and ``fill-consistency.json`` (this run's evidence);
  ``lvs.json`` (``write_lvs_report`` re-run); ``project.kct``, ``HARDWARE.md``
  and ``firmware/`` (current sources, after the firmware check);
  ``renders/`` (carried forward only while the board's copper, footprints and
  graphics are identical, UUIDs aside, to the board they were rendered from);
  ``design-source.zip`` (rebuilt, byte-reproducibly, from the current board
  sources).

BOM changes are refused (the identities in HARDWARE.md/project.kct were
reviewed against it); CPL changes are refused except the reviewed list in
:data:`ACCEPTED_CPL_CHANGES`.  Nothing the current package ships may be
dropped.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

BOARD_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BOARD_DIR.parent))

from release_package import Release, board_content, write_zip  # noqa: E402

from kicad_tools.cli.readiness_cmd import (  # noqa: E402
    FAILED,
    NOT_RUN,
    PASSED,
    CheckOutcome,
    ReadinessOptions,
)

FIRMWARE = ("main.c", "Makefile", "charlieplex.hex")
#: SHA-256 of the firmware sources and programmed image at the compiled audit
#: (readiness inputs of the d95b6eff release).  The firmware check only
#: carries the compile result forward while these bytes are unchanged.
AUDITED_FIRMWARE = {
    "firmware/main.c": "352605415b7d26e64ed0f1e279f43645fd858809b4667382508ac001b38426cd",
    "firmware/Makefile": "8f2c32f565080e8423a872dcb5c236a3a297a705a729f3d2585e0f6b21428f64",
    "firmware/charlieplex.hex": "7956fb57f17750f476cd50460e275e6282ca35babb4b9f62f08c0eedb862c9ad",
}
AUDITED_FLASH_BYTES = 214
AUDITED_RAM_BYTES = 22

#: Reviewed CPL deltas against the d95b6eff package: ref -> (old, new) rotation.
#: The JLCPCB rotation table has always carried ``LED_0805*: 180``; the
#: d95b6eff export did not apply it to library-qualified footprint IDs
#: (``LED_SMD:LED_0805_2012Metric``) until a4fa5481 (#5152) taught
#: ``match_rotation_correction`` to also match the package name.
ACCEPTED_CPL_CHANGES = {f"D{i}": ("0.0", "180.0") for i in range(1, 10)}

RENDERS = ("3d-back.png", "3d-front.png", "pcb-back.svg", "pcb-front.svg")
#: Board-level sources archived in design-source.zip and hashed as inputs.
SOURCE_GLOBS = ("*.py", "*.md", "project.kct")

README_LINES = (
    "",
    "ATtiny85 Charlieplex LED Grid, revision B",
    "-----------------------------------------",
    "  Assemble the sixteen SMT components using bom_jlcpcb.csv and cpl_jlcpcb.csv.",
    "  Hand-solder U1 (ATtiny85-20PU), J1 (power) and J2 (AVR ISP) afterward.",
    "  The BOM includes these three sourcing entries; they are intentionally absent "
    "from the SMT CPL.",
    "  Match the U1 notch/pin1 marker, J1/J2 square pin1 pads, and LED cathode marks "
    "to the drawings.",
    "  Supply regulated 3.3-5.0V to J1 pin1, ground to pin2. Use only one power source.",
    "  J2 pins: 1=MISO,2=VCC,3=SCK,4=MOSI,5=RESET,6=GND.",
    "  Program firmware/charlieplex.hex with AVR ISP at a conservative clock; factory "
    "clock fuses are assumed.",
    "  See HARDWARE.md for full power, programming, pinout and procurement instructions.",
    "  Physical bring-up has not yet been performed.",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hex_flash_bytes(path: Path) -> int:
    """Data bytes of an Intel HEX image, verifying every record checksum."""
    total = 0
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise ValueError(f"{path.name}:{number}: not an Intel HEX record")
        raw = bytes.fromhex(line[1:])
        if sum(raw) & 0xFF:
            raise ValueError(f"{path.name}:{number}: bad record checksum")
        if raw[3] == 0x00:
            total += raw[0]
    return total


def _firmware_check(board: Path) -> CheckOutcome:
    changed = [n for n, digest in AUDITED_FIRMWARE.items() if _sha(board / n) != digest]
    if changed:
        return CheckOutcome(
            "firmware",
            FAILED,
            f"{', '.join(changed)} changed since the compiled audit; recompile and re-audit.",
            blockers=["Firmware differs from the compiled audit."],
        )
    flash = _hex_flash_bytes(board / "firmware/charlieplex.hex")
    if flash != AUDITED_FLASH_BYTES:
        return CheckOutcome(
            "firmware",
            FAILED,
            f"programmed image holds {flash} flash bytes, audit recorded {AUDITED_FLASH_BYTES}.",
            blockers=["Firmware image does not match its audit."],
        )
    detail = (
        f"Sources and programmed image byte-identical to the AVR GCC 7.3.0 compiled audit "
        f"({AUDITED_FLASH_BYTES} flash bytes, {AUDITED_RAM_BYTES} RAM bytes); Intel HEX "
        f"record checksums verified and flash size re-measured ({flash} bytes)."
    )
    if shutil.which("avr-gcc") and shutil.which("make"):
        with tempfile.TemporaryDirectory() as scratch:
            work = Path(scratch) / "firmware"
            shutil.copytree(board / "firmware", work)
            (work / "charlieplex.hex").unlink()
            build = subprocess.run(["make", "-C", str(work)], capture_output=True, text=True)
            rebuilt = work / "charlieplex.hex"
            if build.returncode != 0 or not rebuilt.is_file():
                return CheckOutcome(
                    "firmware",
                    FAILED,
                    "avr-gcc rebuild failed.",
                    blockers=["Firmware rebuild failed."],
                )
            same = rebuilt.read_bytes() == (board / "firmware/charlieplex.hex").read_bytes()
            detail += f" Local avr-gcc rebuild {'matches' if same else 'DIFFERS from'} the image."
            if not same:
                return CheckOutcome(
                    "firmware", FAILED, detail, blockers=["Rebuilt firmware image differs."]
                )
    else:
        detail += " avr-gcc not installed here, so no rebuild."
    return CheckOutcome(
        "firmware", PASSED, detail + " Physical hardware bring-up remains untested."
    )


def _drill_check(options: ReadinessOptions) -> CheckOutcome:
    from kicad_tools.manufacturers import get_profile
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.sexp import parse_file
    from kicad_tools.validate.rules.via_in_pad import ViaInPadRule

    rules = get_profile(options.manufacturer).get_design_rules(layers=2)
    violations = ViaInPadRule().check(PCB.load(options.pcb), rules).violations
    relocations = json.loads((options.evidence_dir / "drill-relocations.json").read_text())
    doc = parse_file(options.pcb)
    names = {n.get_int(0): n.get_string(1) for n in doc.find_children("net")}
    vias = set()
    for via in doc.find_all("via"):
        at, net = via.find("at"), via.find("net")
        name = names.get(net.get_int(0), net.get_string(0)) if net is not None else None
        vias.add((name, round(at.get_float(0), 3), round(at.get_float(1), 3)))
    missing = [
        r["net"]
        for r in relocations
        if (r["net"], *(round(v, 3) for v in r["absolute_to"])) not in vias
    ]
    if violations or missing:
        return CheckOutcome(
            "smt_drill_clearance",
            FAILED,
            f"{len(violations)} drill/SMT-land overlap(s); relocated vias missing: {missing}.",
            evidence="output/readiness/drill-relocations.json",
            blockers=["SMT drill clearance failed."],
        )
    return CheckOutcome(
        "smt_drill_clearance",
        PASSED,
        f"Issue #5012 actual-land overlap rule re-run ({options.manufacturer}, 2 layers): 0 "
        f"overlaps; all {len(relocations)} reviewed via relocations present on their nets. "
        "Ordinary JLCPCB process.",
        evidence="output/readiness/drill-relocations.json",
    )


def _erc_check(options: ReadinessOptions) -> CheckOutcome:
    from kicad_tools.cli.runner import find_kicad_cli

    report = options.evidence_dir / "native-erc.json"
    cli = find_kicad_cli()
    if cli is None or options.schematic is None:
        return CheckOutcome(
            "native_erc",
            NOT_RUN,
            "kicad-cli or schematic unavailable.",
            blockers=["Native ERC not run."],
        )
    subprocess.run(
        [
            str(cli),
            "sch",
            "erc",
            "--format",
            "json",
            "--severity-error",
            "--severity-warning",
            "-o",
            str(report),
            str(options.schematic),
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    data = json.loads(report.read_text())
    count = sum(len(sheet.get("violations", [])) for sheet in data.get("sheets", []))
    if count:
        return CheckOutcome(
            "native_erc",
            FAILED,
            f"{count} native ERC error/warning finding(s).",
            evidence="output/readiness/native-erc.json",
            blockers=["Native ERC findings."],
        )
    return CheckOutcome(
        "native_erc",
        PASSED,
        "Fresh native KiCad ERC: 0 errors, 0 warnings.",
        evidence="output/readiness/native-erc.json",
    )


def board_checks(options: ReadinessOptions) -> list[CheckOutcome]:
    return [_firmware_check(options.board_dir), _drill_check(options), _erc_check(options)]


def _cpl_policy(old: bytes, new: bytes) -> None:
    old_rows = old.decode().splitlines()
    new_rows = new.decode().splitlines()
    if len(old_rows) != len(new_rows):
        raise RuntimeError("cpl_jlcpcb.csv row set changed; re-review placement")
    for o, n in zip(old_rows, new_rows, strict=True):
        if o == n:
            continue
        of, nf = o.split(","), n.split(",")
        ref = of[0]
        accepted = ACCEPTED_CPL_CHANGES.get(ref)
        rest_same = of[:5] == nf[:5] and of[6:] == nf[6:]
        if not (accepted and rest_same and (of[5], nf[5]) == accepted):
            raise RuntimeError(f"cpl_jlcpcb.csv changed for {ref}: {o!r} -> {n!r}; re-review")


def _bom_policy(old: bytes, new: bytes) -> None:
    raise RuntimeError("bom_jlcpcb.csv changed; re-review component identities in HARDWARE.md")


def _renders(name: str):
    def produce(options: ReadinessOptions) -> bytes:
        archive = RELEASE.old("kicad_project.zip")
        with zipfile.ZipFile(__import__("io").BytesIO(archive)) as zf:
            rendered_from = zf.read(options.pcb.name)
        if board_content(rendered_from) != board_content(options.pcb.read_bytes()):
            raise RuntimeError("board content changed since renders/ was made; re-render it")
        return (options.board_dir / "output" / "renders" / name).read_bytes()

    return produce


def _sources(board: Path) -> list[Path]:
    found = {p for pattern in SOURCE_GLOBS for p in board.glob(pattern) if p.is_file()}
    return sorted(found)


def _design_source(options: ReadinessOptions) -> bytes:
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "design-source.zip"
        write_zip(path, {p.name: p.read_bytes() for p in _sources(options.board_dir)})
        return path.read_bytes()


def _lvs(options: ReadinessOptions) -> bytes:
    from kicad_tools.lvs import write_lvs_report

    output = options.board_dir / "output"
    write_lvs_report(
        options.schematic, options.pcb, output, require_clean=True, run_copper=True, run_label=True
    )
    return (output / "lvs.json").read_bytes()


def _copy(relative: str, base: str = "board"):
    def produce(options: ReadinessOptions) -> bytes:
        root = options.evidence_dir if base == "evidence" else options.board_dir
        return (root / relative).read_bytes()

    return produce


def _inputs(options: ReadinessOptions) -> list[Path]:
    board = options.board_dir
    files = [p.relative_to(board) for p in _sources(board)]
    files += [Path("firmware") / n for n in (".gitignore", *FIRMWARE)]
    files += [Path("output/lvs.json")]
    files += [Path("output/renders") / n for n in RENDERS]
    return files


RELEASE = Release(
    board_dir=BOARD_DIR,
    generated={
        "kct-check.json": _copy("kct-check.json", "evidence"),
        "native-erc.json": _copy("native-erc.json", "evidence"),
        "fill-consistency.json": _copy("fill-consistency.json", "evidence"),
        "lvs.json": _lvs,
        "project.kct": _copy("project.kct"),
        "HARDWARE.md": _copy("HARDWARE.md"),
        **{f"firmware/{n}": _copy(f"firmware/{n}") for n in FIRMWARE},
        **{f"renders/{n}": _renders(n) for n in RENDERS},
        "design-source.zip": _design_source,
    },
    checks=board_checks,
    inputs=_inputs,
    readme_lines=README_LINES,
    change_policy={"bom_jlcpcb.csv": _bom_policy, "cpl_jlcpcb.csv": _cpl_policy},
)

if __name__ == "__main__":
    sys.exit(RELEASE.main())

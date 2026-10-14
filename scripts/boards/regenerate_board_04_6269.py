"""Refresh board 04's evidence after the U2 reference-silk move (Issue #6269).

The U2 reference text moved from ``(at 0 -5.5)`` to ``(at 0 -6)`` in both
``generate_design.py`` and the committed routed PCB, clearing the four
``silk_over_copper`` warnings against pads 41-44.  No copper, pad or net
changed, so this script regenerates only what the move touches, from the
already-edited routed PCB (no re-route, no UUID changes):

* ``readiness/native-drc.json`` (copied into the bundle).  It must report 0
  violations of any severity and 0 unconnected items, because that is what
  ``readiness.json``'s ``native_drc`` check claims;
* the ``F_Silkscreen`` member of ``gerbers/gerbers.zip`` (every other member
  keeps its committed bytes);
* ``design-source/`` and ``kicad_project.zip``, then the manifest;
* ``check_manufacturing.py``'s report (0 errors, 0 warnings, manifest PASSED);
* ``output/drc_report.json`` via the recipe's ``run_drc``;
* the outer ``manufacturing.zip``.

Copper is unchanged, so ``fill-consistency.json`` and the native ERC reports
are left as they are.  ``readiness.json`` is then re-issued by the gated
producer::

    uv run --with shapely python scripts/boards/regenerate_board_04_6269.py
    uv run --with shapely python scripts/boards/refresh_readiness.py \\
        boards/04-stm32-devboard --checked-at <ts>

Both need KiCad 10 ``kicad-cli`` on PATH and initialised global library
tables (``scripts/ci/init_kicad_libraries.py``; set ``KICAD_CONFIG_HOME`` to a
scratch directory to avoid touching a shared host's configuration).  Without
them native DRC reports ``lib_footprint_issues`` and this script refuses.
Precedent: ``regenerate_board_04_6404.py``.
"""

from __future__ import annotations

import importlib.util
import json
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
BOARD = REPO_ROOT / "boards" / "04-stm32-devboard"
SILK_MEMBER = re.compile(r"-F_Silkscreen\.[A-Za-z0-9]+$")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


regen = _load("regenerate_boards_03_04_6076", HERE / "regenerate_boards_03_04_6076.py")
refresh_tools = regen.refresh_tools
refresh_gerbers = _load(
    "refresh_copper_gerbers_6404", HERE / "refresh_copper_gerbers_6404.py"
).refresh_copper_gerbers


def main() -> int:
    out, mfg = BOARD / "output", BOARD / "output/manufacturing"
    readiness = out / "readiness"
    routed = out / "stm32_devboard_routed.kicad_pcb"
    regen.drop_bytecode(BOARD)
    sys.path.insert(0, str(BOARD))

    with tempfile.TemporaryDirectory(prefix="regen-04-6269-drc-") as tmp:
        report = Path(tmp) / "native-drc.json"
        subprocess.run(
            ["kicad-cli", "pcb", "drc", "--format", "json", "--severity-error",
             "--severity-warning", "--schematic-parity", "--units", "mm",
             "-o", str(report), str(routed)],
            capture_output=True, check=False,
        )  # fmt: skip
        data = json.loads(report.read_text())
        findings = [v["type"] for v in data["violations"]]
        unconnected = len(data.get("unconnected_items", []))
        parity = len(data.get("schematic_parity", []))
        regen.log(f"  native DRC: {findings or 'no violations'}, {unconnected} unconnected")
        if findings or unconnected or parity:
            raise RuntimeError("native DRC is not clean; refusing to write evidence")
        shutil.copy2(report, readiness / "native-drc.json")
    shutil.copy2(readiness / "native-drc.json", mfg / "native-drc.json")

    replaced = refresh_gerbers(routed, mfg / "gerbers/gerbers.zip", SILK_MEMBER)
    regen.log(f"  silkscreen gerbers replaced: {replaced}")
    regen.refresh_design_source(BOARD)
    changed = refresh_tools.rebuild_archive(mfg / "kicad_project.zip", [out, BOARD])
    regen.log(f"  kicad_project.zip rebuilt; changed members {changed}")
    refresh_tools.refresh_manifest_files(mfg)

    check = regen._load("board04_check_manufacturing", BOARD / "check_manufacturing.py")
    with tempfile.TemporaryDirectory(prefix="regen-04-6269-check-") as tmp:
        data = check.check(routed, Path(tmp) / "check-report.json")
        report_bytes = (Path(tmp) / "check-report.json").read_bytes()
    regen.log(
        f"  check_manufacturing: {data['summary']['errors']} errors, "
        f"{data['summary']['warnings']} warnings, overall {data['meta_checks']['overall']}"
    )
    regen.require_manifest_passed(data, "board 04 check-report.json")
    if data["summary"]["errors"] or data["summary"]["warnings"]:
        raise RuntimeError("check_manufacturing reports findings; refusing to write evidence")
    (mfg / "check-report.json").write_bytes(report_bytes)
    (readiness / "kct-check.json").write_bytes(report_bytes)
    refresh_tools.refresh_manifest_files(mfg)

    recipe = runpy.run_path(str(BOARD / "generate_design.py"), run_name="regen_04_6269")
    regen.log(f"  recipe run_drc: {recipe['run_drc'](routed)}")
    regen.require_manifest_passed(
        json.loads((out / "drc_report.json").read_text()), "board 04 drc_report.json"
    )
    refresh_tools.rebuild_archive(out / "manufacturing.zip", [mfg])
    regen.drop_bytecode(BOARD)
    regen.log("  now re-issue readiness.json with scripts/boards/refresh_readiness.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Refresh board 04's hash-bound evidence after its zone refill (Issue #6404).

``declare_router_netclasses_6390.py --refill 04-stm32-devboard`` recomputes the
saved zone fills under the router's net-class rules.  Board 04 hash-binds the
exact routed PCB bytes (``kicad_project.zip`` + manifest, Gerbers plotted from
the saved fills), so this script regenerates, in order, and only from the
already-refilled routed PCB (no re-route, no UUID changes):

* ``readiness/{native-drc,fill-consistency}.json`` (copied into the bundle);
* the F.Cu / B.Cu members of ``gerbers/gerbers.zip`` (only the copper layers
  carry the fill; every other member, including the merged drill file, keeps
  the committed bytes);
* ``kicad_project.zip`` and the manifest;
* ``check_manufacturing.py``'s report (must pass the manifest meta-check);
* ``output/drc_report.json`` via the recipe's ``run_drc``;
* the outer ``manufacturing.zip``.

The schematic is untouched, so the native ERC reports are left as they are.
Precedent: ``regenerate_boards_03_04_6076.py``.

Usage (repository root, KiCad 10 ``kicad-cli`` on PATH)::

    uv run --with shapely python scripts/boards/regenerate_board_04_6404.py
"""

from __future__ import annotations

import importlib.util
import json
import runpy
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
BOARD = REPO_ROOT / "boards" / "04-stm32-devboard"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


regen = _load("regenerate_boards_03_04_6076", HERE / "regenerate_boards_03_04_6076.py")
refresh_tools = regen.refresh_tools
refresh_copper_gerbers = _load(
    "refresh_copper_gerbers_6404", HERE / "refresh_copper_gerbers_6404.py"
).refresh_copper_gerbers


def main() -> int:
    out, mfg = BOARD / "output", BOARD / "output/manufacturing"
    readiness = out / "readiness"
    routed = out / "stm32_devboard_routed.kicad_pcb"
    regen.drop_bytecode(BOARD)
    sys.path.insert(0, str(BOARD))

    subprocess.run(
        ["kicad-cli", "pcb", "drc", "--format", "json", "--severity-error",
         "--severity-warning", "--schematic-parity", "--units", "mm",
         "-o", str(readiness / "native-drc.json"), str(routed)],
        capture_output=True, check=False,
    )  # fmt: skip
    delta = regen.fill_consistency(routed)
    (readiness / "fill-consistency.json").write_text(json.dumps(delta, indent=2) + "\n")
    regen.log(f"  fill consistency (saved vs independent refill): {delta}")
    for name in ("native-drc.json", "fill-consistency.json"):
        shutil.copy2(readiness / name, mfg / name)

    regen.log(
        f"  copper gerbers replaced: {refresh_copper_gerbers(routed, mfg / 'gerbers/gerbers.zip')}"
    )
    changed = refresh_tools.rebuild_archive(mfg / "kicad_project.zip", [out, BOARD])
    regen.log(f"  kicad_project.zip rebuilt; changed members {changed}")
    refresh_tools.refresh_manifest_files(mfg)

    check = regen._load("board04_check_manufacturing", BOARD / "check_manufacturing.py")
    with tempfile.TemporaryDirectory(prefix="regen-04-check-") as tmp:
        data = check.check(routed, Path(tmp) / "check-report.json")
        report = (Path(tmp) / "check-report.json").read_bytes()
    regen.log(
        f"  check_manufacturing: {data['summary']['errors']} errors, "
        f"{data['summary']['warnings']} warnings, overall {data['meta_checks']['overall']}"
    )
    regen.require_manifest_passed(data, "board 04 check-report.json")
    (mfg / "check-report.json").write_bytes(report)
    (readiness / "kct-check.json").write_bytes(report)
    refresh_tools.refresh_manifest_files(mfg)

    recipe = runpy.run_path(str(BOARD / "generate_design.py"), run_name="regen_04")
    regen.log(f"  recipe run_drc: {recipe['run_drc'](routed)}")
    regen.require_manifest_passed(
        json.loads((out / "drc_report.json").read_text()), "board 04 drc_report.json"
    )
    refresh_tools.rebuild_archive(out / "manufacturing.zip", [mfg])
    regen.drop_bytecode(BOARD)
    regen.log("  readiness.json NOT re-pinned (legacy format, no producer; see #6269 / #6020)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

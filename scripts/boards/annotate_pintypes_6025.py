"""Carry schematic pad pin types onto boards 04-07's committed PCBs (Issue #6025).

Each board's generator now calls ``annotate_pcb_file_pintypes`` right after it
writes the unrouted PCB.  These boards ship routed copper that is reviewed
and hash-bound (board 04's post-route surgery, board 05's fingerprinted
``routing.json`` replay plus its #6395 repairs, board 06's seeded negotiated
route, board 07's reviewed-routing fixture).  Re-running their routers would
churn copper that the annotation does not touch, so this script follows
``regenerate_boards_03_04_6076.py``: no re-route.

For each board it

1. regenerates the unrouted PCB with the board's own generator in a scratch
   directory and publishes it, refusing unless it differs from the committed
   file only by the ``(pinfunction ...)`` / ``(pintype ...)`` pad children
   (board 07's committed unrouted copy has no producer; it is annotated in
   place under the same check);
2. annotates the routed PCB the same way the router would have inherited it,
   under the same pintype-only check;
3. re-plots the Gerber bundle and swaps in only the members whose content
   changed.  The only allowed change is the X2 pad attribute
   ``%TO.P,<ref>,<pad>[,<pinfunction>]*%`` (plus the plot date header);
   anything else refuses.  Board 07 is re-plotted by
   ``refresh_bundles_6404.py`` instead (its ``build.py`` publishes the routed
   PCB), so it is skipped here;
4. board 04 only: refreshes the evidence ``refresh_readiness.py`` does not
   (native DRC, design-source, ``kicad_project.zip`` and manifest,
   ``check_manufacturing.py``'s report, the recipe's ``drc_report.json`` and
   the outer archive), exactly as ``regenerate_board_04_6269.py`` does.

Then re-pin each board's ``readiness.json`` with its producer::

    uv run --with shapely python scripts/boards/annotate_pintypes_6025.py 04 05 06 07
    uv run --with shapely python scripts/boards/refresh_readiness.py \\
        boards/04-stm32-devboard boards/05-bldc-motor-controller \\
        boards/06-diffpair-test --checked-at <ts>
    uv run --with shapely python scripts/boards/refresh_bundles_6404.py \\
        boards/07-matchgroup-test --checked-at <ts>

Needs KiCad 10 ``kicad-cli`` on PATH with initialised library tables
(``scripts/ci/init_kicad_libraries.py``).  Rerunning is idempotent: once a
board is annotated every step is a no-op.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
BOARDS = REPO_ROOT / "boards"

from kicad_tools.operations.pintype import annotate_pcb_file_pintypes  # noqa: E402

PIN_CHILD = re.compile(r'\s*\(pin(?:function|type) "(?:[^"\\]|\\.)*"\)')
#: Gerber/drill header lines that carry the plot date or the KiCad build string
#: (CI's image reports ``10.0.6-10.0.6~ubuntu24.04.1``, the docker image
#: ``10.0.6``), not board content.
DATE_LINE = re.compile(
    rb"CreationDate|GenerationSoftware|^G04 Created by |^; DRILL file |date \d{4}-"
)
#: Copper layers: the only members that carry X2 pad attributes.
COPPER_MEMBER = re.compile(r"-(?:F|B|In\d+)_Cu\.[A-Za-z0-9]+$")
#: X2 pad attribute; the pin function is appended once pads carry one.
PAD_ATTRIBUTE = re.compile(rb"^%TO\.P,")


def log(message: str) -> None:
    print(message, flush=True)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def without_pin_children(text: str) -> str:
    return PIN_CHILD.sub("", text)


def require_pin_only_change(before: str, after: str, what: str) -> None:
    if without_pin_children(before) != without_pin_children(after):
        raise RuntimeError(f"{what}: changes beyond pad pinfunction/pintype; refusing")


# --------------------------------------------------------------------------- generators
def _generate_04(tmp: Path) -> Path:
    recipe = runpy.run_path(
        str(BOARDS / "04-stm32-devboard/generate_design.py"), run_name="annotate_6025_04"
    )
    recipe["create_project"](tmp, "stm32_devboard")
    sch = recipe["create_stm32_schematic"](tmp)
    pcb = recipe["create_stm32_pcb"](tmp)
    # The same call main() makes between create_stm32_pcb() and route_pcb().
    annotation = recipe["annotate_pcb_file_pintypes"](pcb, sch)
    if annotation.missing_pads:
        raise RuntimeError(f"board 04: pads without a schematic pin: {annotation.missing_pads}")
    return pcb


def _generate_05(tmp: Path) -> Path:
    redesign = BOARDS / "05-bldc-motor-controller/redesign"
    sys.path.insert(0, str(redesign))
    try:
        hardware = _load("annotate_6025_05_hardware", redesign / "hardware.py")
        hardware.generate(tmp)
    finally:
        sys.path.remove(str(redesign))
    return tmp / "bldc_controller.kicad_pcb"


def _generate_06(tmp: Path) -> Path:
    script = BOARDS / "06-diffpair-test/assembled-demo/generate.py"
    subprocess.run([sys.executable, str(script), str(tmp)], check=True, capture_output=True)
    return tmp / "diffpair_test.kicad_pcb"


@dataclass(frozen=True)
class Board:
    directory: str
    schematic: str
    unrouted: str
    routed: tuple[str, ...]
    #: Writes the unrouted PCB into a scratch directory (None: no producer).
    generate: Callable[[Path], Path] | None
    #: The committed Gerber bundle re-plotted here (None: a later driver re-plots it).
    gerbers: str | None = "output/manufacturing/gerbers/gerbers.zip"


FAMILIES: dict[str, Board] = {
    "04": Board(
        "04-stm32-devboard",
        "output/stm32_devboard.kicad_sch",
        "output/stm32_devboard.kicad_pcb",
        ("output/stm32_devboard_routed.kicad_pcb",),
        _generate_04,
    ),
    "05": Board(
        "05-bldc-motor-controller",
        "output/bldc_controller.kicad_sch",
        "output/bldc_controller.kicad_pcb",
        ("output/bldc_controller_routed.kicad_pcb",),
        _generate_05,
    ),
    "06": Board(
        "06-diffpair-test",
        "output/diffpair_test.kicad_sch",
        "output/diffpair_test.kicad_pcb",
        ("output/diffpair_test_routed.kicad_pcb",),
        _generate_06,
    ),
    # real_design/build.py annotates the routed candidate it validates, and
    # refresh_bundles_6404.py publishes it (with output/readiness/checked.*).
    # The committed unrouted copy predates #6396 and has no producer.
    "07": Board(
        "07-matchgroup-test",
        "output/matchgroup_test.kicad_sch",
        "output/matchgroup_test.kicad_pcb",
        (),
        None,
        gerbers=None,
    ),
}


# --------------------------------------------------------------------------- stages
def stage_unrouted(board: Path, spec: Board) -> None:
    committed = board / spec.unrouted
    before = committed.read_text()
    if spec.generate is None:
        result = annotate_pcb_file_pintypes(committed, board / spec.schematic)
        if result.missing_pads:
            raise RuntimeError(f"{spec.unrouted}: pads without a pin: {result.missing_pads}")
        require_pin_only_change(before, committed.read_text(), spec.unrouted)
        log(f"  {spec.unrouted}: annotated in place ({result.updated} pads updated)")
        return
    with tempfile.TemporaryDirectory(prefix=f"annotate-6025-{spec.directory}-") as tmp:
        fresh = spec.generate(Path(tmp))
        text = fresh.read_text()
    require_pin_only_change(before, text, f"{spec.unrouted} (fresh generator run)")
    if text != before:
        committed.write_text(text)
    log(f"  {spec.unrouted}: regenerated ({'changed' if text != before else 'unchanged'})")


def stage_routed(board: Path, spec: Board) -> None:
    for name in spec.routed:
        pcb = board / name
        before = pcb.read_text()
        result = annotate_pcb_file_pintypes(pcb, board / spec.schematic)
        if result.missing_pads:
            raise RuntimeError(f"{name}: pads without a schematic pin: {result.missing_pads}")
        require_pin_only_change(before, pcb.read_text(), name)
        log(f"  {name}: {result.updated} pads updated, {result.annotated} annotated")


def _content(data: bytes, *, ignore_pads: bool) -> list[bytes]:
    return [
        line
        for line in data.splitlines()
        if not DATE_LINE.search(line) and not (ignore_pads and PAD_ATTRIBUTE.search(line))
    ]


def stage_gerbers(board: Path, spec: Board) -> list[str]:
    """Swap in the re-plotted members whose X2 pad attributes changed."""
    from kicad_tools.export.gerber import GerberExporter

    archive = board / spec.gerbers
    routed = board / spec.routed[0]
    with tempfile.TemporaryDirectory(prefix="annotate-6025-gerbers-") as tmp:
        fresh_zip = GerberExporter(routed).export_for_manufacturer("jlcpcb", Path(tmp))
        with zipfile.ZipFile(fresh_zip) as fresh:
            fresh_members = {name: fresh.read(name) for name in fresh.namelist()}
    with zipfile.ZipFile(archive) as old:
        entries = [(info, old.read(info.filename)) for info in old.infolist()]
    replaced: list[str] = []
    out: list[tuple[zipfile.ZipInfo, bytes]] = []
    for info, data in entries:
        fresh = fresh_members.get(info.filename)
        if fresh is None or not COPPER_MEMBER.search(info.filename):
            # Drill, mask, silk, outline and job files carry no pad pin data;
            # they keep their committed bytes (the committed merged drill
            # file has no counterpart in a fresh PTH/NPTH plot).
            out.append((info, data))
            continue
        if _content(data, ignore_pads=True) != _content(fresh, ignore_pads=True):
            raise RuntimeError(
                f"{info.filename}: the fresh plot differs beyond X2 pad attributes; refusing"
            )
        if _content(data, ignore_pads=False) != _content(fresh, ignore_pads=False):
            data = fresh
            replaced.append(info.filename)
        out.append((info, data))
    if replaced:
        with zipfile.ZipFile(archive, "w") as new:
            for info, data in out:
                new.writestr(info, data, compress_type=info.compress_type)
    log(f"  {spec.gerbers}: replaced {replaced or 'nothing'}")
    return replaced


def stage_board04_evidence(board: Path) -> None:
    """``regenerate_board_04_6269.py``'s evidence refresh, minus its silk re-plot."""
    regen = _load("regenerate_boards_03_04_6076", HERE / "regenerate_boards_03_04_6076.py")
    refresh_tools = regen.refresh_tools
    out, mfg = board / "output", board / "output/manufacturing"
    readiness = out / "readiness"
    routed = out / "stm32_devboard_routed.kicad_pcb"
    regen.drop_bytecode(board)
    sys.path.insert(0, str(board))

    with tempfile.TemporaryDirectory(prefix="annotate-6025-04-drc-") as tmp:
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
        log(f"  native DRC: {findings or 'no violations'}, {unconnected} unconnected")
        if findings or unconnected or parity:
            raise RuntimeError("native DRC is not clean; refusing to write evidence")
        shutil.copy2(report, readiness / "native-drc.json")
    shutil.copy2(readiness / "native-drc.json", mfg / "native-drc.json")

    regen.refresh_design_source(board)
    changed = refresh_tools.rebuild_archive(mfg / "kicad_project.zip", [out, board])
    log(f"  kicad_project.zip rebuilt; changed members {changed}")
    refresh_tools.refresh_manifest_files(mfg)

    check = regen._load("board04_check_manufacturing", board / "check_manufacturing.py")
    with tempfile.TemporaryDirectory(prefix="annotate-6025-04-check-") as tmp:
        data = check.check(routed, Path(tmp) / "check-report.json")
        report_bytes = (Path(tmp) / "check-report.json").read_bytes()
    log(
        f"  check_manufacturing: {data['summary']['errors']} errors, "
        f"{data['summary']['warnings']} warnings, overall {data['meta_checks']['overall']}"
    )
    regen.require_manifest_passed(data, "board 04 check-report.json")
    if data["summary"]["errors"] or data["summary"]["warnings"]:
        raise RuntimeError("check_manufacturing reports findings; refusing to write evidence")
    (mfg / "check-report.json").write_bytes(report_bytes)
    (readiness / "kct-check.json").write_bytes(report_bytes)
    refresh_tools.refresh_manifest_files(mfg)

    recipe = runpy.run_path(str(board / "generate_design.py"), run_name="annotate_6025_04_drc")
    log(f"  recipe run_drc: {recipe['run_drc'](routed)}")
    regen.require_manifest_passed(
        json.loads((out / "drc_report.json").read_text()), "board 04 drc_report.json"
    )
    refresh_tools.rebuild_archive(out / "manufacturing.zip", [mfg])
    regen.drop_bytecode(board)


def annotate_family(key: str) -> None:
    spec = FAMILIES[key]
    board = BOARDS / spec.directory
    log(f"board {spec.directory}")
    stage_unrouted(board, spec)
    stage_routed(board, spec)
    if spec.gerbers is not None:
        stage_gerbers(board, spec)
    if key == "04":
        stage_board04_evidence(board)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("families", nargs="+", choices=sorted(FAMILIES))
    args = parser.parse_args(argv)
    for key in args.families:
        annotate_family(key)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Native identity correlation must detect a broken critical connection."""

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.schema.pcb import PCB

ROOT = Path(__file__).resolve().parents[1] / "boards/09-usbc-pd-power"

# find_kicad_cli() checks PATH plus common non-PATH install locations, so
# this can't silently disagree with the rest of the suite about which
# kicad-cli (if any) is available (#5714).
KICAD_CLI = find_kicad_cli()


@pytest.mark.skipif(
    KICAD_CLI is None,
    reason="find_kicad_cli() found no kicad-cli install (checked PATH and common install locations)",
)
def test_native_gate_detects_removed_boot_route(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    spec = importlib.util.spec_from_file_location("board09_check", ROOT / "check_design.py")
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    output = tmp_path / "output"
    shutil.copytree(ROOT / "output", output)
    path = output / "usbc_pd_power.kicad_pcb"
    report = output / "test-drc.json"

    def check_opens():
        subprocess.run(
            [str(KICAD_CLI), "pcb", "drc", str(path), "--format", "json", "--output", str(report)],
            check=True,
            capture_output=True,
            text=True,
        )
        return checker.critical_route_opens(path, json.loads(report.read_text()))

    assert check_opens() == []
    board = PCB.load(path)
    removed = {s.uuid for s in board.segments if s.net_name == "BOOT"}
    assert removed, "Fixture must contain the bootstrap connection being tested"
    board._sexp.children = [
        n
        for n in board._sexp.children
        if not (n.name == "segment" and n.get("uuid").get_string(0) in removed)
    ]
    board.save(path)
    assert check_opens() == ["BOOT"]
    board = PCB.load(path)
    assert checker.inner_ground_planes_valid(board)
    board.add_trace((40, 58), (41, 58), net="SDA", layer="In1.Cu", width=0.2)
    assert not checker.inner_ground_planes_valid(board)

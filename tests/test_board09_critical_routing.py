"""Native identity correlation must detect a broken critical connection."""

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB

ROOT = Path(__file__).resolve().parents[1] / "boards/09-usbc-pd-power"


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="requires native KiCad DRC")
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
            ["kicad-cli", "pcb", "drc", str(path), "--format", "json", "--output", str(report)],
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

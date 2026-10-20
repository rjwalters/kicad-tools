"""Board 05 legacy generator: MOSFET symbol pin numbers match footprint pads (#6290)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from kicad_tools.lvs.board_lvs import compare_netlists

DESIGN = (
    Path(__file__).resolve().parent.parent / "boards" / "05-bldc-motor-controller" / "design.py"
)


@pytest.mark.skipif(not DESIGN.exists(), reason="board 05 design.py missing")
def test_q1_q6_pads_bind_without_pad_name_mismatches(tmp_path):
    spec = importlib.util.spec_from_file_location("board05_design_6290", DESIGN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.create_project(tmp_path, "bldc_controller")
    sch = mod.create_bldc_controller(tmp_path)
    pcb = mod.create_bldc_pcb(tmp_path)
    result = compare_netlists(sch, pcb)
    q_mismatches = [m for m in result.mismatches if m.ref.startswith("Q")]
    assert q_mismatches == []

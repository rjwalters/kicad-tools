"""Every committed fleet CPL is in the Gerber/drill frame (Issue #7199).

#7059 moved ``kct export``'s CPL coordinates into the frame the Gerbers and
drill files use, but the committed packages kept the old Edge.Cuts-top-left,
Y-down frame, so a fab loading one would place every part wrongly.  The
expectation here comes from the routed PCB's raw footprint positions
(``scripts/boards/_cpl_frame.py``), not from ``kct export``, so a bug shared
with the exporter cannot hide.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "boards" / "_cpl_frame.py"

spec = importlib.util.spec_from_file_location("cpl_frame_script", SCRIPT)
cpl_frame = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cpl_frame
spec.loader.exec_module(cpl_frame)

BOARDS = sorted(
    p.parent.parent.parent
    for p in REPO.glob("boards/*/output/manufacturing/cpl_jlcpcb.csv")
    if (p.parent / "manifest.json").is_file()
)


def test_fleet_cpls_are_discovered():
    assert len(BOARDS) >= 8


#: Boards whose producers still refuse for reasons unrelated to the CPL (#7241).
STILL_OLD_FRAME = {"05-bldc-motor-controller", "07-matchgroup-test"}


def _board_param(board):
    marks = (
        [pytest.mark.xfail(strict=True, reason="old-frame CPL, regeneration blocked: #7241")]
        if board.name in STILL_OLD_FRAME
        else []
    )
    return pytest.param(board, id=board.name, marks=marks)


@pytest.mark.parametrize("board", [_board_param(b) for b in BOARDS])
def test_committed_cpl_matches_gerber_frame(board):
    manufacturing = board / "output" / "manufacturing"
    manifest = json.loads((manufacturing / "manifest.json").read_text())
    pcb = board / "output" / manifest["board"]["pcb_file"]
    rows = cpl_frame.read_cpl(manufacturing / "cpl_jlcpcb.csv")
    assert rows, f"{board.name}: empty CPL"
    assert cpl_frame.coordinate_mismatches(rows, pcb) == []


def test_frame_check_catches_the_old_frame(tmp_path):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(
        "(kicad_pcb (setup (aux_axis_origin 100 50))"
        ' (footprint "R" (at 110 60) (property "Reference" "R1")))'
    )
    good = [{"Designator": "R1", "Mid X": "10.0000mm", "Mid Y": "-10.0000mm"}]
    old_frame = [{"Designator": "R1", "Mid X": "10.0000mm", "Mid Y": "10.0000mm"}]
    assert cpl_frame.coordinate_mismatches(good, pcb) == []
    assert len(cpl_frame.coordinate_mismatches(old_frame, pcb)) == 1
    assert cpl_frame.coordinate_mismatches([{**good[0], "Mid X": "10.0099mm"}], pcb) == []
    assert len(cpl_frame.coordinate_mismatches([{**good[0], "Mid X": "10.02mm"}], pcb)) == 1

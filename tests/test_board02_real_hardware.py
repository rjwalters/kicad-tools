"""The demonstration MCU is a physical, powered and programmable ATtiny85."""

import importlib.util
import sys
from pathlib import Path

import pytest

from kicad_tools.lvs.board_lvs import compare_netlists

BOARD_DIR = Path(__file__).resolve().parents[1] / "boards" / "02-charlieplex-led"


@pytest.fixture
def hardware(monkeypatch):
    monkeypatch.syspath_prepend(str(BOARD_DIR))
    # Other board recipes also use bare sibling-module names.
    monkeypatch.delitem(sys.modules, "design_spec", raising=False)
    spec = importlib.util.spec_from_file_location(
        "board02_hardware", BOARD_DIR / "hardware_design.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_dip_pinout_and_programming_connections(hardware):
    parts = {part[0]: part for part in hardware.components()}
    assert len(parts) == 19
    assert parts["U1"][2:5] == (
        "ATtiny85-20PU",
        "Package_DIP:DIP-8_W7.62mm",
        {
            "1": "RESET",
            "2": "LINE_A",
            "3": "LINE_B",
            "4": "GND",
            "5": "LINE_C",
            "6": "LINE_D",
            "7": "ISP_SCK",
            "8": "VCC",
        },
    )
    assert parts["J2"][4] == {
        "1": "LINE_D",
        "2": "VCC",
        "3": "ISP_SCK",
        "4": "LINE_C",
        "5": "RESET",
        "6": "GND",
    }
    for ref in ("C1", "C2"):
        assert parts[ref][4] == {"1": "VCC", "2": "GND"}
    assert parts["R5"][4] == {"1": "VCC", "2": "RESET"}

    pcb = hardware.build_pcb()
    mcu = next(fp for fp in pcb.footprints if fp.reference == "U1")
    pads = {pad.number: pad for pad in mcu.pads}
    assert len(pads) == 8
    assert pads["8"].position[0] - pads["1"].position[0] == pytest.approx(7.62)
    assert pads["4"].position[1] - pads["1"].position[1] == pytest.approx(7.62)
    assert all(pad.type == "thru_hole" for pad in pads.values())


def test_generated_real_schematic_and_pcb_agree(hardware, tmp_path):
    schematic = hardware.write_schematic(tmp_path / "board.kicad_sch")
    pcb = hardware.write_pcb(tmp_path / "board.kicad_pcb")
    result = compare_netlists(schematic, pcb)
    assert result.clean, result.mismatches
    assert not result.vacuous

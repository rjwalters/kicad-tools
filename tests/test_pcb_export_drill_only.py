"""PCB.export_drill() writes drill files only -- no Gerbers, no zip (Issue #6180)."""

from pathlib import Path

import pytest

from kicad_tools.export import find_kicad_cli
from kicad_tools.schema.pcb import PCB

BOARD = Path(__file__).parent.parent / "boards/00-simple-led/output/simple_led_routed.kicad_pcb"

pytestmark = [
    pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli not available"),
    pytest.mark.skipif(not BOARD.exists(), reason="demo board not generated"),
]


@pytest.fixture
def pcb():
    return PCB.load(BOARD)


def test_only_drill_files_and_directory_returned(pcb, tmp_path):
    result = pcb.export_drill(tmp_path / "d")
    assert result == tmp_path / "d"
    assert result.is_dir()
    names = sorted(p.name for p in result.iterdir())
    assert names and all(n.endswith(".drl") for n in names), names
    assert not list(result.glob("*.zip"))


def test_pth_npth_split_default(pcb, tmp_path):
    out = pcb.export_drill(tmp_path)
    names = {p.name for p in out.glob("*.drl")}
    assert f"{BOARD.stem}-PTH.drl" in names
    assert f"{BOARD.stem}.drl" not in names


def test_merge_pth_npth(pcb, tmp_path):
    out = pcb.export_drill(tmp_path, merge_pth_npth=True)
    assert [p.name for p in out.iterdir()] == [f"{BOARD.stem}.drl"]


def test_units_honoured(pcb, tmp_path):
    mm = pcb.export_drill(tmp_path / "mm", units="mm")
    inch = pcb.export_drill(tmp_path / "in", units="in")
    pth = f"{BOARD.stem}-PTH.drl"
    assert "METRIC" in (mm / pth).read_text()
    assert "INCH" in (inch / pth).read_text()

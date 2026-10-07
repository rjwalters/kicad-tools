"""Board 03 footprint silk meets the jlcpcb-tier1 stroke floor (Issue #5762)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kicad_tools.drc.repair_silkscreen import FP_GRAPHIC_TYPES, SilkscreenRepairer

BOARDS = Path(__file__).resolve().parent.parent / "boards"
FLOOR = 0.15
_SILK_WIDTH = re.compile(
    r"\((fp_line|fp_rect|fp_circle|fp_arc|fp_poly)\b(?:(?!\n\t\t\(fp_|\n\t\t\(pad|\n\t\)).)*?"
    r"\(width ([0-9.]+)\)(?:(?!\n\t\t\(fp_|\n\t\t\(pad|\n\t\)).)*?\(layer \"[FB]\.SilkS\"\)",
    re.S,
)


def test_fp_poly_is_repaired():
    assert "fp_poly" in FP_GRAPHIC_TYPES


@pytest.mark.parametrize("name", ["usb_joystick.kicad_pcb", "usb_joystick_routed.kicad_pcb"])
def test_board03_artifacts_have_no_undersized_silk(name: str):
    path = BOARDS / "03-usb-joystick" / "output" / name
    result = SilkscreenRepairer(path).repair_line_widths(FLOOR, dry_run=True)
    assert result.total_fixed == 0, result.fixes[:3]
    widths = [float(m.group(2)) for m in _SILK_WIDTH.finditer(path.read_text())]
    assert len(widths) == 139
    assert min(widths) >= FLOOR


@pytest.mark.parametrize(
    "board,name",
    [
        ("02-charlieplex-led", "charlieplex_led_routed.kicad_pcb"),
        ("05-bldc-motor-controller", None),
        ("06-diffpair-test", None),
        ("07-matchgroup-test", None),
    ],
)
def test_other_boards_unchanged_by_fp_poly_support(board: str, name: str | None):
    """Growing FP_GRAPHIC_TYPES must not touch any other demo board."""
    outputs = sorted((BOARDS / board / "output").glob("*_routed.kicad_pcb"))
    if not outputs:
        pytest.skip(f"{board}: no routed artifact")
    for path in outputs:
        result = SilkscreenRepairer(path).repair_line_widths(FLOOR, dry_run=True)
        assert result.total_fixed == 0, (path, result.fixes[:3])

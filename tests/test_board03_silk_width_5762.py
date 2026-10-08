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


# --- Regeneration parity (Judge review of PR #6200) -------------------------
#
# The joint re-attach that keeps J3's nudged pin-1 marker joined to its body
# outline used to edit the pre-clip neighbour node, which the clip then
# replaced -- so the committed bytes (produced by re-running the pass on an
# already-clipped board) were joined while every fresh ``write_pcb()`` call
# left a 0.01 mm gap and a J3/J3 ``silk_overlap``.  Pin the committed silk to
# what the generator actually emits so that can never drift again.

_SILK_LAYERS = {"F.SilkS", "B.SilkS"}
_GRAPHICS = ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly")


def _silk_geometry(path: Path) -> list[tuple]:
    """Every footprint silk graphic as (ref, type, layer, points, width), UUID-free."""
    from kicad_tools.sexp import parse_string

    out: list[tuple] = []
    for fp in parse_string(path.read_text()).children:
        if getattr(fp, "name", None) != "footprint":
            continue
        ref = next(
            (
                c.get_string(1)
                for c in fp.children
                if getattr(c, "name", None) == "property" and c.get_string(0) == "Reference"
            ),
            None,
        )
        for child in fp.children:
            if getattr(child, "name", None) not in _GRAPHICS:
                continue
            layer = child.find_child("layer")
            if layer is None or layer.get_string(0) not in _SILK_LAYERS:
                continue
            points = tuple(
                (tag, child.find_child(tag).get_float(0), child.find_child(tag).get_float(1))
                for tag in ("start", "mid", "end", "center")
                if child.find_child(tag) is not None
            )
            stroke = child.find_child("stroke")
            width = stroke.find_child("width").get_float(0) if stroke is not None else None
            out.append((ref, child.name, layer.get_string(0), points, width))
    return sorted(out, key=repr)


@pytest.fixture(scope="module")
def regenerated_board03(tmp_path_factory) -> Path:
    import subprocess
    import sys

    board = BOARDS / "03-usb-joystick"
    generated = tmp_path_factory.mktemp("board03") / "usb_joystick.kicad_pcb"
    proc = subprocess.run(
        [sys.executable, str(board / "generate_pcb.py"), str(generated)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=str(board),
    )
    if proc.returncode != 0 and "Required real KiCad footprint unavailable" in (
        proc.stdout + proc.stderr
    ):
        pytest.skip("KiCad footprint libraries not installed; generator cannot run")
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    return generated


@pytest.mark.parametrize("name", ["usb_joystick.kicad_pcb", "usb_joystick_routed.kicad_pcb"])
def test_committed_silk_matches_fresh_write_pcb(regenerated_board03: Path, name: str):
    committed = BOARDS / "03-usb-joystick" / "output" / name
    assert _silk_geometry(regenerated_board03) == _silk_geometry(committed)


def test_regenerated_j3_pin1_marker_stays_joined(regenerated_board03: Path):
    """No J3/J3 ``silk_overlap``: the nudged marker and the outline share a corner."""
    from kicad_tools.manufacturers import get_profile
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.rules.silkscreen import check_silk_overlap

    rules = get_profile("jlcpcb-tier1").get_design_rules(layers=4)
    violations = check_silk_overlap(PCB.load(regenerated_board03), rules).violations
    j3_self = [
        v.message
        for v in violations
        if v.message.startswith("Silkscreen J3 (fp_line)")
        and "silkscreen J3 (fp_line)" in v.message
    ]
    assert j3_self == []

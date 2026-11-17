"""silk_over_mask_opening quotes sheet-absolute coordinates (Issue #8879)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.manufacturers import get_profile  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.validate.rules.silkscreen import check_silk_over_mask_opening  # noqa: E402

_HEAD = """(kicad_pcb (version 20240108) (generator "test") (generator_version "9.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user) (37 "F.SilkS" user)
          (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user))
  (net 0 "")
"""

_COORD = re.compile(r"at \((-?\d+\.\d+), (-?\d+\.\d+)\)")


def _run(tmp_path: Path, body: str, edge: str):
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_HEAD + edge + body + ")\n")
    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1.0)
    pcb = PCB.load(str(path))
    return check_silk_over_mask_opening(pcb, rules), pcb


def _edge(x0: float, y0: float) -> str:
    return (
        f"  (gr_rect (start {x0} {y0}) (end {x0 + 100} {y0 + 100}) "
        '(stroke (width 0.1) (type solid)) (fill none) (layer "Edge.Cuts"))\n'
    )


_SILK = (
    '  (gr_text "SILK TEXT" (at {x} {y}) (layer "F.SilkS")'
    " (effects (font (size 1 1) (thickness 0.15))))\n"
)


@pytest.mark.parametrize("origin", [(0.0, 0.0), (120.0, 90.0)])
def test_gr_opening_quotes_sheet_coordinates(tmp_path: Path, origin: tuple[float, float]) -> None:
    ox, oy = origin
    cx, cy = ox + 19.04, oy + 61.0
    body = (
        f"  (gr_circle (center {cx} {cy}) (end {cx + 2} {cy}) "
        '(stroke (width 0) (type solid)) (fill yes) (layer "F.Mask"))\n'
    ) + _SILK.format(x=cx, y=cy)
    res, pcb = _run(tmp_path, body, _edge(ox, oy))
    assert tuple(pcb._board_origin) == pytest.approx(origin)
    assert len(res.violations) == 1
    m = _COORD.search(res.violations[0].message)
    assert m, res.violations[0].message
    assert (float(m.group(1)), float(m.group(2))) == pytest.approx((cx, cy), abs=0.01)


@pytest.mark.parametrize("origin", [(0.0, 0.0), (120.0, 90.0)])
def test_fp_opening_quotes_sheet_coordinates(tmp_path: Path, origin: tuple[float, float]) -> None:
    ox, oy = origin
    cx, cy = ox + 30.0, oy + 40.0
    body = (
        f'  (footprint "FID" (layer "F.Cu") (at {cx} {cy})\n'
        "    (fp_circle (center 0 0) (end 2 0) (stroke (width 0) (type solid)) (fill yes)\n"
        '      (layer "F.Mask")))\n'
    ) + _SILK.format(x=cx, y=cy)
    res, _ = _run(tmp_path, body, _edge(ox, oy))
    assert len(res.violations) == 1
    m = _COORD.search(res.violations[0].message)
    assert m, res.violations[0].message
    assert (float(m.group(1)), float(m.group(2))) == pytest.approx((cx, cy), abs=0.01)

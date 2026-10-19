"""THT parts export at their pad-bbox centre, not pin 1 (#7433)."""

from __future__ import annotations

import csv
import io

import pytest

from kicad_tools.export.pnp import export_pnp
from kicad_tools.schema.pcb import PCB

_PADS = "".join(
    f'(pad "{i + 1}" thru_hole circle (at 0 {i * 2.54:.2f}) (size 1.7 1.7) (drill 1.0)'
    ' (layers "*.Cu" "*.Mask"))'
    for i in range(8)
)


def _pcb(angle: int) -> str:
    return f"""(kicad_pcb (version 20240108) (generator "test")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (footprint "PinHeader_1x08" (layer "F.Cu") (at 100 50 {angle}) (attr through_hole)
    (property "Reference" "J1") (property "Value" "Conn") {_PADS})
)
"""


@pytest.mark.parametrize(
    ("angle", "dx", "dy"),
    # KiCad negated-angle convention: local +Y (down the header) rotates to
    # (sin t, cos t) in board space.
    [(0, 0.0, 8.89), (90, 8.89, 0.0), (180, 0.0, -8.89), (270, -8.89, 0.0)],
)
def test_header_exports_at_pad_centroid(tmp_path, angle, dx, dy):
    path = tmp_path / "h.kicad_pcb"
    path.write_text(_pcb(angle))
    pcb = PCB.load(path)
    rows = list(csv.DictReader(io.StringIO(export_pnp(pcb.footprints, "jlcpcb", pcb_path=path))))
    row = rows[0]
    x = float(row["Mid X"].removesuffix("mm"))
    y = float(row["Mid Y"].removesuffix("mm"))
    # CPL frame is Y up with aux origin 0: y_out = -(50 + dy)
    assert x == pytest.approx(100 + dx, abs=1e-3)
    assert y == pytest.approx(-(50 + dy), abs=1e-3)

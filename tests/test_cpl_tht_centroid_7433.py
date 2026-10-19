"""THT parts export at their pad-bbox centre, not pin 1 (#7433).

The expected offsets below follow KiCad's negated-angle footprint rotation
(``math.radians(-rot)``, issue #3739): a footprint-local offset ``(lx, ly)``
lands at ``(lx*cos t + ly*sin t, -lx*sin t + ly*cos t)`` in board space
(Y down).  Every case was cross-checked black-box against the pinned
kicad-cli 10.0.6 image: the CPL Mid X/Y equals the bbox centre of the
footprint's holes in ``kicad-cli pcb export drill`` output, top and bottom.
"""

from __future__ import annotations

import csv
import io
import math

import pytest

from kicad_tools.export.pnp import export_pnp
from kicad_tools.schema.pcb import PCB

_PITCH = 2.54
_SPAN = 7 * _PITCH / 2  # 8.89 mm: pin 1 to the centre of a 1x8 header


def _pads(angle: int, sign: int) -> str:
    # KiCad writes pad rotation as absolute (footprint rotation included).
    # ``sign=-1`` stores the pads with local Y negated, which is what a
    # top/bottom flip writes for a bottom-side header.
    return "".join(
        f'(pad "{i + 1}" thru_hole circle (at 0 {sign * i * _PITCH:.2f} {angle})'
        ' (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask"))'
        for i in range(8)
    )


def _pcb(angle: int, layer: str = "F.Cu", sign: int = 1) -> str:
    return f"""(kicad_pcb (version 20240108) (generator "test")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (footprint "PinHeader_1x08" (layer "{layer}") (at 100 50 {angle}) (attr through_hole)
    (property "Reference" "J1") (property "Value" "Conn") {_pads(angle, sign)})
)
"""


def _expected(angle: int, sign: int) -> tuple[float, float]:
    """Board-frame (Y down) centroid offset, computed from the #3739 formula."""
    theta = math.radians(angle)
    ly = sign * _SPAN
    return (ly * math.sin(theta), ly * math.cos(theta))


# (layer, angle, sign, dx, dy): dx/dy are the literal board-frame offsets,
# spelled out so a sign error in the formula cannot hide in ``_expected``.
_CASES = [
    ("F.Cu", 0, 1, 0.0, _SPAN),
    ("F.Cu", 90, 1, _SPAN, 0.0),
    ("F.Cu", 180, 1, 0.0, -_SPAN),
    ("F.Cu", 270, 1, -_SPAN, 0.0),
    ("B.Cu", 0, 1, 0.0, _SPAN),
    ("B.Cu", 90, 1, _SPAN, 0.0),
    ("B.Cu", 180, 1, 0.0, -_SPAN),
    ("B.Cu", 270, 1, -_SPAN, 0.0),
    ("B.Cu", 0, -1, 0.0, -_SPAN),
    ("B.Cu", 90, -1, -_SPAN, 0.0),
    ("B.Cu", 180, -1, 0.0, _SPAN),
    ("B.Cu", 270, -1, _SPAN, 0.0),
]
_IDS = [
    f"{layer}-{angle}-{'flipped' if sign < 0 else 'plain'}" for layer, angle, sign, *_ in _CASES
]


def _load(tmp_path, angle: int, layer: str, sign: int) -> tuple[PCB, object]:
    path = tmp_path / "h.kicad_pcb"
    path.write_text(_pcb(angle, layer, sign))
    return PCB.load(path), path


@pytest.mark.parametrize(("layer", "angle", "sign", "dx", "dy"), _CASES, ids=_IDS)
def test_tht_centroid_offset_numeric(tmp_path, layer, angle, sign, dx, dy):
    """``_tht_centroid_offset`` matches the negated-angle convention (#3739)."""
    from kicad_tools.export.pnp import _tht_centroid_offset

    assert _expected(angle, sign) == pytest.approx((dx, dy), abs=1e-9)
    pcb, _ = _load(tmp_path, angle, layer, sign)
    (fp,) = pcb.footprints
    assert fp.layer == layer
    assert _tht_centroid_offset(fp) == pytest.approx((dx, dy), abs=1e-6)


@pytest.mark.parametrize(("layer", "angle", "sign", "dx", "dy"), _CASES, ids=_IDS)
def test_header_exports_at_pad_centroid(tmp_path, layer, angle, sign, dx, dy):
    pcb, path = _load(tmp_path, angle, layer, sign)
    rows = list(csv.DictReader(io.StringIO(export_pnp(pcb.footprints, "jlcpcb", pcb_path=path))))
    row = rows[0]
    assert row["Layer"].lower() == ("top" if layer == "F.Cu" else "bottom")
    x = float(row["Mid X"].removesuffix("mm"))
    y = float(row["Mid Y"].removesuffix("mm"))
    # CPL frame is Y up with aux origin 0: y_out = -(50 + dy).  Bottom-side
    # rows are not mirrored: Mid X/Y stay in the top-view board frame.
    assert x == pytest.approx(100 + dx, abs=1e-3)
    assert y == pytest.approx(-(50 + dy), abs=1e-3)

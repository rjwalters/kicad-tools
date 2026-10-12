"""Issue #6402: a bbox island must not swallow an incumbent pour's pads.

Mirrors board 03: authored ``VCC`` pour on ``In2.Cu``; ``kct route``'s
auto-pour gives ``VBUS`` a pad-bbox island on the same layer.  The VBUS
bbox encloses VCC pads (U1 pin 44, C1), cutting them off from the VCC
plane.  The island must be carved so VCC's pads are not covered while
VBUS's own pads still are.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("shapely")

from shapely.geometry import Point, Polygon  # noqa: E402

from kicad_tools.router.net_class import NetClass  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.zones.generator import auto_create_zones_for_pour_nets  # noqa: E402

_HEADER = """\
(kicad_pcb
  (version 20240108)
  (generator "kicad")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "VCC")
  (net 3 "VBUS")
  (net 4 "SIG")
  (gr_rect (start 0 0) (end 60 40) (stroke (width 0.15) (type solid))
    (fill none) (layer "Edge.Cuts") (uuid "edge-uuid"))
"""


def _fp(ref: str, x: float, y: float, num: int, net: str) -> str:
    return f"""  (footprint "T:{ref}" (layer "F.Cu") (at {x} {y}) (uuid "fp-{ref}")
    (property "Reference" "{ref}" (at 0 -2 0) (layer "F.SilkS") (uuid "r-{ref}")
      (effects (font (size 1 1) (thickness 0.15))))
    (property "Value" "T" (at 0 2 0) (layer "F.Fab") (uuid "v-{ref}")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net {num} "{net}"))
  )
"""


def _zone(uuid: str, num: int, net: str, layer: str, prio: int) -> str:
    return f"""  (zone (net {num}) (net_name "{net}") (layer "{layer}") (uuid "{uuid}")
    (hatch edge 0.5) (priority {prio}) (connect_pads (clearance 0.4))
    (min_thickness 0.25) (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 1 1) (xy 59 1) (xy 59 39) (xy 1 39))))
"""


def _board(path: Path, vcc_pads: list[tuple[float, float]]) -> Path:
    vbus = [(10.0, 10.0), (30.0, 10.0), (20.0, 20.0)]
    rows = [_fp(f"J{i}", x, y, 3, "VBUS") for i, (x, y) in enumerate(vbus)]
    rows += [_fp(f"U{i}", x, y, 2, "VCC") for i, (x, y) in enumerate(vcc_pads)]
    rows += [_fp("R1", 50, 30, 4, "SIG"), _fp("R2", 5, 35, 4, "SIG")]
    path.write_text(
        _HEADER
        + "".join(rows)
        + _zone("z-vcc", 2, "VCC", "In2.Cu", 0)
        + _zone("z-gnd", 1, "GND", "In1.Cu", 0)
        + ")\n"
    )
    return path


@pytest.mark.parametrize(
    "vcc_pads",
    [
        [(20.0, 10.0)],  # interior of the VBUS bbox (needs a notch)
        [(20.0, 10.0), (21.0, 15.0)],  # two interior VCC pads
        [(31.0, 18.0)],  # near the bbox edge
    ],
)
def test_vbus_island_does_not_cover_vcc_pads(tmp_path: Path, vcc_pads) -> None:
    path = _board(tmp_path / "b.kicad_pcb", vcc_pads)
    auto_create_zones_for_pour_nets(path, [("VBUS", NetClass.POWER)])
    pcb = PCB.load(str(path))
    zones = [z for z in pcb.zones if z.net_name == "VBUS"]
    assert len(zones) == 1
    zone = zones[0]
    assert zone.layer == "In2.Cu"
    poly = Polygon(zone.polygon)
    assert poly.is_valid and poly.area > 0
    for x, y in [(10.0, 10.0), (30.0, 10.0), (20.0, 20.0)]:
        assert poly.contains(Point(x, y)), f"VBUS pad ({x},{y}) lost"
    for x, y in vcc_pads:
        assert not poly.intersects(Point(x, y).buffer(0.25)), f"VCC pad ({x},{y}) covered"

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

from shapely.geometry import Point, Polygon, box  # noqa: E402

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


def _fp(ref: str, x: float, y: float, num: int, net: str, size: float = 0.6) -> str:
    return f"""  (footprint "T:{ref}" (layer "F.Cu") (at {x} {y}) (uuid "fp-{ref}")
    (property "Reference" "{ref}" (at 0 -2 0) (layer "F.SilkS") (uuid "r-{ref}")
      (effects (font (size 1 1) (thickness 0.15))))
    (property "Value" "T" (at 0 2 0) (layer "F.Fab") (uuid "v-{ref}")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at 0 0) (size {size} {size}) (layers "F.Cu") (net {num} "{net}"))
  )
"""


def _zone(uuid: str, num: int, net: str, layer: str, prio: int) -> str:
    return f"""  (zone (net {num}) (net_name "{net}") (layer "{layer}") (uuid "{uuid}")
    (hatch edge 0.5) (priority {prio}) (connect_pads (clearance 0.4))
    (min_thickness 0.25) (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 1 1) (xy 59 1) (xy 59 39) (xy 1 39))))
"""


VBUS_PADS = [(10.0, 10.0), (30.0, 10.0), (20.0, 20.0)]
VCC_PAD_SIZE = 1.2

# The VBUS island's priority-1 fill keeps 0.3 mm (its zone clearance) from
# the VCC fill, whose min_thickness is 0.25 mm: VCC copper exists only where
# the free space survives an erosion of 0.3 + 0.125 mm.
FILL_EROSION_MM = 0.3 + 0.125


def _board(
    path: Path,
    vcc_pads: list[tuple[float, float]],
    vbus: list[tuple[float, float]] = VBUS_PADS,
) -> Path:
    rows = [_fp(f"J{i}", x, y, 3, "VBUS") for i, (x, y) in enumerate(vbus)]
    rows += [_fp(f"U{i}", x, y, 2, "VCC", VCC_PAD_SIZE) for i, (x, y) in enumerate(vcc_pads)]
    rows += [_fp("R1", 50, 30, 4, "SIG"), _fp("R2", 5, 35, 4, "SIG")]
    path.write_text(
        _HEADER
        + "".join(rows)
        + _zone("z-vcc", 2, "VCC", "In2.Cu", 0)
        + _zone("z-gnd", 1, "GND", "In1.Cu", 0)
        + ")\n"
    )
    return path


def _vbus_island(path: Path) -> Polygon:
    auto_create_zones_for_pour_nets(path, [("VBUS", NetClass.POWER)])
    pcb = PCB.load(str(path))
    zones = [z for z in pcb.zones if z.net_name == "VBUS"]
    assert len(zones) == 1
    assert zones[0].layer == "In2.Cu"
    poly = Polygon(zones[0].polygon)
    assert poly.is_valid and poly.area > 0
    assert len(poly.interiors) == 0
    return poly


def _assert_vcc_reaches_plane(poly: Polygon, vcc_pads) -> None:
    """Each VCC pad body is uncovered and VCC plane fill can reach it.

    The VCC plane fill is the board minus the VBUS island, eroded by the
    zone clearance and half the min_thickness.  Its main piece (the plane)
    must come within 1 mm of every VCC pad body -- a carve too narrow for
    fill to flow through (the original 0.6 mm notch) leaves the pad on an
    isolated sliver or with no fill at all.
    """
    free = box(0, 0, 60, 40).difference(poly).buffer(-FILL_EROSION_MM)
    parts = list(getattr(free, "geoms", [free]))
    plane = max(parts, key=lambda g: g.area)
    half = VCC_PAD_SIZE / 2.0
    for x, y in vcc_pads:
        body = box(x - half, y - half, x + half, y + half)
        assert not poly.intersects(body), f"VCC pad body at ({x},{y}) covered"
        gap = plane.distance(body)
        assert gap < 1.0, f"VCC plane fill {gap:.2f} mm from pad ({x},{y})"


@pytest.mark.parametrize(
    "vcc_pads",
    [
        [(20.0, 10.0)],  # interior of the VBUS bbox (needs a notch)
        [(20.0, 10.0), (21.0, 15.0)],  # two interior VCC pads
        [(31.0, 18.0)],  # near the bbox edge
    ],
)
def test_vbus_island_does_not_cover_vcc_pads(tmp_path: Path, vcc_pads) -> None:
    poly = _vbus_island(_board(tmp_path / "b.kicad_pcb", vcc_pads))
    for x, y in VBUS_PADS:
        assert poly.contains(Point(x, y)), f"VBUS pad ({x},{y}) lost"
    _assert_vcc_reaches_plane(poly, vcc_pads)


def test_channel_avoids_own_pad_on_nearest_side(tmp_path: Path) -> None:
    """Board 03's F1/C12 shape: the nearest edge is behind an own pad.

    The VCC pad (F1.2's role) is nearest the island's right edge, but a
    VBUS pad (C12.1's role) sits between it and that edge.  Channelling
    right would cut the VBUS pad out, so the carve must open the VCC pad
    toward another edge, and still leave a channel wide enough for fill.
    """
    vbus = [(10.0, 10.0), (28.0, 10.0), (20.0, 20.0), (28.0, 14.0)]
    vcc_pads = [(25.0, 14.0)]
    poly = _vbus_island(_board(tmp_path / "b.kicad_pcb", vcc_pads, vbus))
    for x, y in vbus:
        assert poly.contains(Point(x, y)), f"VBUS pad ({x},{y}) lost"
    _assert_vcc_reaches_plane(poly, vcc_pads)

"""Issue #6410: multi-net thermal / blanket stitch must gate each net on its OWN fill.

#6400 fixed ``run_stitch``: in a multi-net call the "same-net" fill pool held
the fill of EVERY target net and the "other-net" pool excluded every target
net, so a via for net N could land on (thermal) or drill through (blanket)
sibling target net M's pour.  ``run_thermal_stitch`` and
``run_blanket_stitch`` built the same two pools and had the same bug.  Both
now split the pools per net via ``_split_fill_pools`` (the hoisted #6400
helper), so each net is gated exactly as a single-net call would gate it.

Thermal fixture: In2.Cu carries a VCC fill on the right (x >= 125) and a VBUS
fill rectangle on the left.  Q1 is a VCC power pad sitting over the VBUS fill;
Q2 a VBUS pad over the VBUS fill; Q3 a VCC pad over the VCC fill.

Blanket fixture: VCC fill covers all of In2.Cu; a VBUS fill rectangle sits on
In1.Cu.  Blanket vias span F.Cu..In2.Cu, so they cross In1.Cu and must clear
the VBUS pour.  Blanket stitch shares ``same_net_via_positions`` across nets,
which hides the bug for one net order -- so both orders are tested.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from kicad_tools.cli.stitch_cmd import (
    point_in_polygon,
    run_blanket_stitch,
    run_thermal_stitch,
)

VIA_SIZE = 0.45
DRILL = 0.2
CLEARANCE = 0.2


def _sq(a: float, b: float, c: float, d: float) -> list[tuple[float, float]]:
    return [(a, b), (c, b), (c, d), (a, d)]


VBUS_FILL = _sq(104, 104, 124, 136)
VCC_THERMAL_FILL = _sq(125, 100, 140, 140)
VCC_BLANKET_FILL = _sq(100, 100, 140, 140)

_HDR = """(kicad_pcb (version 20240108) (generator "test") (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no)) (paper "A4")
  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "") (net 1 "VBUS") (net 2 "VCC")
"""


def _pts(points: list[tuple[float, float]]) -> str:
    return " ".join(f"(xy {x} {y})" for x, y in points)


def _zone(net: int, name: str, layer: str, outline, fill, prio: int) -> str:
    return f"""  (zone (net {net}) (net_name "{name}") (layer "{layer}") (uuid "z-{name}-{layer}")
    (name "{name}_{layer}") (priority {prio}) (connect_pads (clearance 0.2))
    (min_thickness 0.2) (fill yes)
    (polygon (pts {_pts(outline)}))
    (filled_polygon (layer "{layer}") (pts {_pts(fill)})))
"""


def _power_fp(ref: str, x: float, y: float, net: int, name: str) -> str:
    return f"""  (footprint "Package_TO_SOT_SMD:TO-252-2" (layer "F.Cu") (uuid "fp-{ref}") (at {x} {y})
    (property "Reference" "{ref}" (at 0 -3 0) (layer "F.SilkS") (uuid "ref-{ref}"))
    (pad "2" smd rect (at 0 0) (size 3 3) (layers "F.Cu" "F.Paste" "F.Mask") (net {net} "{name}")))
"""


THERMAL_PCB = (
    _HDR
    + _zone(2, "VCC", "In2.Cu", _sq(100, 100, 140, 140), VCC_THERMAL_FILL, 0)
    + _zone(1, "VBUS", "In2.Cu", VBUS_FILL, VBUS_FILL, 1)
    + _power_fp("Q1", 115, 120, 2, "VCC")
    + _power_fp("Q2", 110, 110, 1, "VBUS")
    + _power_fp("Q3", 132, 120, 2, "VCC")
    + ")\n"
)

BLANKET_PCB = (
    _HDR
    + _zone(2, "VCC", "In2.Cu", VCC_BLANKET_FILL, VCC_BLANKET_FILL, 0)
    + _zone(1, "VBUS", "In1.Cu", VBUS_FILL, VBUS_FILL, 0)
    + ")\n"
)


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "board.kicad_pcb"
    p.write_text(text)
    return p


def _thermal(tmp_path: Path, nets: list[str]):
    return run_thermal_stitch(
        pcb_path=_write(tmp_path, THERMAL_PCB),
        net_names=nets,
        via_size=VIA_SIZE,
        drill=DRILL,
        clearance=CLEARANCE,
        target_layer="In2.Cu",
        dry_run=True,
    )


def _blanket(tmp_path: Path, nets: list[str]):
    return run_blanket_stitch(
        pcb_path=_write(tmp_path, BLANKET_PCB),
        net_names=nets,
        via_size=VIA_SIZE,
        drill=DRILL,
        clearance=CLEARANCE,
        target_layer="In2.Cu",
        spacing=3.0,
        dry_run=True,
    )


def _dist_to_polygon_edge(x: float, y: float, poly: list[tuple[float, float]]) -> float:
    best = math.inf
    n = len(poly)
    for i in range(n):
        (ax, ay), (bx, by) = poly[i], poly[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
        best = min(best, math.hypot(x - (ax + t * dx), y - (ay + t * dy)))
    return best


def _violates(v, poly: list[tuple[float, float]]) -> bool:
    """Via pad inside ``poly`` or within ``via_size/2 + clearance`` of it."""
    if point_in_polygon(v.via_x, v.via_y, poly):
        return True
    return _dist_to_polygon_edge(v.via_x, v.via_y, poly) < VIA_SIZE / 2 + CLEARANCE


def _positions(result, net: str) -> list[tuple[float, float]]:
    return sorted(
        (round(v.via_x, 6), round(v.via_y, 6)) for v in result.vias_added if v.pad.net_name == net
    )


# --------------------------------------------------------------------------
# Thermal
# --------------------------------------------------------------------------


@pytest.mark.parametrize("nets", [["VBUS", "VCC"], ["VCC", "VBUS"]])
def test_thermal_multi_net_never_lands_on_sibling_fill(tmp_path: Path, nets: list[str]) -> None:
    result = _thermal(tmp_path, nets)
    bad = [
        v
        for v in result.vias_added
        if v.pad.net_name == "VCC" and point_in_polygon(v.via_x, v.via_y, VBUS_FILL)
    ]
    assert not bad, f"VCC vias placed on VBUS fill: {[(v.via_x, v.via_y) for v in bad]}"

    single = _thermal(tmp_path, ["VCC"])
    single_q1 = [r for p, r in single.pads_skipped if p.reference == "Q1"]
    multi_q1 = [r for p, r in result.pads_skipped if p.reference == "Q1"]
    assert single_q1, "fixture sanity: single-net VCC call must skip Q1"
    assert multi_q1 == single_q1


@pytest.mark.parametrize("nets", [["VBUS", "VCC"], ["VCC", "VBUS"]])
def test_thermal_multi_net_matches_single_net(tmp_path: Path, nets: list[str]) -> None:
    multi = _thermal(tmp_path, nets)
    for net in ("VCC", "VBUS"):
        (tmp_path / net).mkdir()
        single = _thermal(tmp_path / net, [net])
        assert _positions(multi, net) == _positions(single, net)


def test_thermal_still_stitches_own_fill(tmp_path: Path) -> None:
    result = _thermal(tmp_path, ["VBUS", "VCC"])
    q2 = [v for v in result.vias_added if v.pad.reference == "Q2"]
    q3 = [v for v in result.vias_added if v.pad.reference == "Q3"]
    assert q2 and all(point_in_polygon(v.via_x, v.via_y, VBUS_FILL) for v in q2)
    assert q3 and all(point_in_polygon(v.via_x, v.via_y, VCC_THERMAL_FILL) for v in q3)


# --------------------------------------------------------------------------
# Blanket
# --------------------------------------------------------------------------


@pytest.mark.parametrize("nets", [["VCC", "VBUS"], ["VBUS", "VCC"]])
def test_blanket_multi_net_clears_sibling_fill_on_other_layer(
    tmp_path: Path, nets: list[str]
) -> None:
    result = _blanket(tmp_path, nets)
    fills = {"VCC": VCC_BLANKET_FILL, "VBUS": VBUS_FILL}
    for v in result.vias_added:
        sibling = "VBUS" if v.pad.net_name == "VCC" else "VCC"
        assert not _violates(v, fills[sibling]), (
            f"{v.pad.net_name} via at ({v.via_x}, {v.via_y}) hits {sibling} fill"
        )


def test_blanket_still_stitches_own_fill(tmp_path: Path) -> None:
    margin = VIA_SIZE / 2 + CLEARANCE
    for nets in (["VCC", "VBUS"], ["VBUS", "VCC"]):
        (tmp_path / nets[0]).mkdir()
        result = _blanket(tmp_path / nets[0], nets)
        vcc = [v for v in result.vias_added if v.pad.net_name == "VCC"]
        assert any(v.via_x > 124 + margin for v in vcc), nets


def test_blanket_single_net_unchanged(tmp_path: Path) -> None:
    """Single-net VCC output is the order-independent reference for multi-net VCC."""
    single = _blanket(tmp_path, ["VCC"])
    assert single.vias_added
    assert not any(_violates(v, VBUS_FILL) for v in single.vias_added)

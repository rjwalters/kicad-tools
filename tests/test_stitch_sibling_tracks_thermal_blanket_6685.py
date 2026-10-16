"""Issue #6685: --thermal / --blanket must treat sibling target nets' existing
tracks and vias as obstacles (mirror of the #6668 fix for run_stitch)."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from kicad_tools.cli.stitch_cmd import run_blanket_stitch, run_thermal_stitch

VIA_SIZE = 0.45
DRILL = 0.2
CLEARANCE = 0.2
SIB_VIA = (117.4, 118.6)
SIB_TRACK = ((110.0, 121.6), (120.0, 121.6))
SIB_W = 0.25
OWN_VIA = (112.0, 117.0)
OWN_TRACK = ((110.0, 117.0), (120.0, 117.0))


def _sq(a, b, c, d):
    return [(a, b), (c, b), (c, d), (a, d)]


def _pts(points) -> str:
    return " ".join(f"(xy {x} {y})" for x, y in points)


_FILL = _sq(100, 100, 140, 140)
_HDR = """(kicad_pcb (version 20240108) (generator "test") (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no)) (paper "A4")
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "") (net 1 "GND") (net 2 "+3V3")
"""


def _zone(net: int, name: str) -> str:
    fill = _FILL if net == 1 else _sq(100, 100, 104, 104)
    return f"""  (zone (net {net}) (net_name "{name}") (layer "B.Cu") (uuid "z-{name}")
    (name "{name}") (priority {net}) (connect_pads (clearance 0.2))
    (min_thickness 0.2) (fill yes)
    (polygon (pts {_pts(fill)}))
    (filled_polygon (layer "B.Cu") (pts {_pts(fill)})))
"""


def _via(p, net: int, tag: str) -> str:
    return (
        f'  (via (at {p[0]} {p[1]}) (size {VIA_SIZE}) (drill {DRILL}) (layers "F.Cu" "B.Cu") '
        f'(net {net}) (uuid "v-{tag}"))\n'
    )


def _track(seg, net: int, tag: str) -> str:
    return (
        f"  (segment (start {seg[0][0]} {seg[0][1]}) (end {seg[1][0]} {seg[1][1]}) "
        f'(width {SIB_W}) (layer "F.Cu") (net {net}) (uuid "s-{tag}"))\n'
    )


_PAD_FP = """  (footprint "Package_TO_SOT_SMD:TO-252-2" (layer "F.Cu") (uuid "fp-Q1") (at 115 120)
    (property "Reference" "Q1" (at 0 -3 0) (layer "F.SilkS") (uuid "ref-Q1"))
    (pad "2" smd rect (at 0 0) (size 3 3) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND")))
"""


def _pcb(thermal: bool, own: bool) -> str:
    text = _HDR + _zone(1, "GND") + _zone(2, "+3V3")
    if thermal:
        text += _PAD_FP
    text += _track(SIB_TRACK, 2, "sib") + _via(SIB_VIA, 2, "sib")
    if own:
        text += _track(OWN_TRACK, 1, "own") + _via(OWN_VIA, 1, "own")
    return text + ")\n"


def _run(tmp_path: Path, mode: str, nets: list[str], own: bool = False):
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_pcb(mode == "thermal", own))
    kw = {
        "pcb_path": pcb,
        "net_names": nets,
        "via_size": VIA_SIZE,
        "drill": DRILL,
        "clearance": CLEARANCE,
        "target_layer": "B.Cu",
        "dry_run": True,
    }
    if mode == "thermal":
        return run_thermal_stitch(thermal_radius=3.5, vias_per_pad=8, **kw)
    return run_blanket_stitch(spacing=1.5, **kw)


def _gnd(result) -> list[tuple[float, float]]:
    return sorted(
        (round(v.via_x, 6), round(v.via_y, 6)) for v in result.vias_added if v.pad.net_name == "GND"
    )


def _seg_dist(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _assert_clear_of_sibling(points) -> None:
    for p in points:
        track_gap = _seg_dist(p, *SIB_TRACK) - SIB_W / 2 - VIA_SIZE / 2
        via_gap = math.hypot(p[0] - SIB_VIA[0], p[1] - SIB_VIA[1]) - VIA_SIZE
        assert track_gap >= CLEARANCE - 1e-3, (p, track_gap)
        assert via_gap >= CLEARANCE - 1e-3, (p, via_gap)


@pytest.mark.parametrize("mode", ["thermal", "blanket"])
def test_two_net_run_clears_sibling_copper(tmp_path: Path, mode: str) -> None:
    multi = _gnd(_run(tmp_path, mode, ["GND", "+3V3"]))
    assert multi, "fixture must place GND vias"
    _assert_clear_of_sibling(multi)
    # Matches a GND-only run against the same copper.
    assert multi == _gnd(_run(tmp_path, mode, ["GND"]))


@pytest.mark.parametrize("mode", ["thermal", "blanket"])
def test_own_net_copper_does_not_block_own_vias(tmp_path: Path, mode: str) -> None:
    multi = _gnd(_run(tmp_path, mode, ["GND", "+3V3"], own=True))
    assert multi
    assert multi == _gnd(_run(tmp_path, mode, ["GND"], own=True))
    _assert_clear_of_sibling(multi)

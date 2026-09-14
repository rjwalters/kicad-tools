"""Physical KiCad orientation, independent of the production rotation helper (#5227)."""

import json
import math
import shutil
import subprocess

import pytest
from shapely.geometry import Point

from kicad_tools.schema.pcb import Footprint, Pad, Via
from kicad_tools.sexp import parse_string
from kicad_tools.validate.rules.clearance import (
    CopperElement,
    _calculate_clearance,
    _pad_polygon,
)


def _point(center, local, degrees):
    """Explicit board-coordinate forward transform; no production geometry calls."""
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    x, y = local
    return center[0] + x * c + y * s, center[1] - x * s + y * c


def _parsed_pad(shape, angle, position=(0, 0)):
    return Pad.from_sexp(
        parse_string(
            f'(pad "1" smd {shape} (at {position[0]} {position[1]} {angle}) '
            '(size 4 1) (layers "F.Cu") (roundrect_rratio 0.25) (net 1 "A"))'
        )
    )


def _footprint(pad, angle=0):
    return Footprint(
        name="Test",
        layer="F.Cu",
        position=(10, 20),
        rotation=angle,
        reference="U1",
        value="Test",
        pads=[pad],
    )


def _via(point):
    return CopperElement.from_via(
        Via(
            position=point, size=0.2, drill=0.1, layers=["F.Cu", "B.Cu"], net_number=2, net_name="B"
        )
    )


def _round_pad(point):
    pad = Pad.from_sexp(
        parse_string('(pad "1" smd circle (at 0 0) (size 0.2 0.2) (layers "F.Cu") (net 2 "B"))')
    )
    fp = _footprint(pad)
    fp.position = point
    return CopperElement.from_pad(pad, fp)


@pytest.mark.parametrize("shape", ["rect", "roundrect", "oval"])
@pytest.mark.parametrize("angle", [-45, -30, 0, 30, 45, 90, 180, 270])
def test_parsed_pad_interior_and_exterior_follow_physical_axes(shape, angle):
    pad = _parsed_pad(shape, angle)
    fp = _footprint(pad)
    polygon = _pad_polygon(pad, fp)
    inside = _point(fp.position, (1.5, 0), angle)
    outside = _point(fp.position, (0, 0.9), angle)
    assert polygon.contains(Point(inside))
    assert not polygon.covers(Point(outside))
    element = CopperElement.from_pad(pad, fp)
    for probe in (_via, _round_pad):
        assert _calculate_clearance(element, probe(inside))[0] < 0
        assert _calculate_clearance(element, probe(outside))[0] > 0.15
    if angle in (-45, -30, 30, 45):
        mirrored = _point(fp.position, (1.5, 0), -angle)
        assert not polygon.covers(Point(mirrored))
        for probe in (_via, _round_pad):
            assert _calculate_clearance(element, probe(mirrored))[0] > 0.15


@pytest.mark.parametrize("shape", ["rect", "roundrect", "oval"])
@pytest.mark.parametrize("angle", [-30, 30])
def test_pad_absolute_angle_is_not_added_to_translated_footprint_angle(shape, angle):
    pad = _parsed_pad(shape, angle, position=(2, 1))
    fp = _footprint(pad, angle=40)
    center = _point(fp.position, pad.position, fp.rotation)
    polygon = _pad_polygon(pad, fp)
    assert polygon.centroid.x == pytest.approx(center[0])
    assert polygon.centroid.y == pytest.approx(center[1])
    assert polygon.contains(Point(_point(center, (1.5, 0), angle)))
    assert not polygon.covers(Point(_point(center, (1.5, 0), -angle)))


@pytest.mark.parametrize("angle", [-45, -30, 0, 30, 45, 90, 180, 270])
def test_circular_pad_rotation_is_invariant(angle):
    pad = Pad.from_sexp(
        parse_string(f'(pad "1" smd circle (at 0 0 {angle}) (size 1 1) (layers "F.Cu"))')
    )
    polygon = _pad_polygon(pad, _footprint(pad))
    assert polygon.contains(Point(10.3, 20))
    assert not polygon.covers(Point(10.7, 20))
    assert polygon.area == pytest.approx(_pad_polygon(_parsed_circle(), _footprint(pad)).area)


def _parsed_circle():
    return Pad.from_sexp(parse_string('(pad "1" smd circle (at 0 0) (size 1 1) (layers "F.Cu"))'))


@pytest.mark.parametrize("shape", ["rect", "roundrect", "oval"])
@pytest.mark.parametrize("angle", [-30, 30])
def test_native_kicad_confirms_physical_overlap_and_mirrored_gap(tmp_path, shape, angle):
    cli = shutil.which("kicad-cli")
    if cli is None:
        pytest.skip("native kicad-cli unavailable")
    inside = _point((10, 20), (1.5, 0), angle)
    outside = _point((30, 20), (1.5, 0), -angle)
    inside_id = "00000000-0000-4000-8000-000000005227"
    outside_id = "00000000-0000-4000-8000-000000005228"
    pcb = tmp_path / "orientation.kicad_pcb"
    pcb.write_text(f'''(kicad_pcb (version 20240108) (generator "pcbnew")
      (general (thickness 1.6)) (paper "A4")
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
      (setup (pad_to_mask_clearance 0)) (net 0 "") (net 1 "A") (net 2 "B")
      (footprint "Test" (layer "F.Cu") (at 10 20)
        (property "Reference" "U1" (at 0 0) (layer "F.SilkS"))
        (pad "1" smd {shape} (at 0 0 {angle}) (size 4 1)
          (layers "F.Cu") (roundrect_rratio 0.25) (net 1 "A")))
      (footprint "Test" (layer "F.Cu") (at 30 20)
        (property "Reference" "U2" (at 0 0) (layer "F.SilkS"))
        (pad "1" smd {shape} (at 0 0 {angle}) (size 4 1)
          (layers "F.Cu") (roundrect_rratio 0.25) (net 1 "A")))
      (footprint "Probe" (layer "F.Cu") (at {inside[0]} {inside[1]})
        (property "Reference" "P1" (at 0 0) (layer "F.SilkS"))
        (pad "1" smd circle (at 0 0) (size 0.2 0.2)
          (layers "F.Cu") (net 2 "B") (uuid "{inside_id}")))
      (footprint "Probe" (layer "F.Cu") (at {outside[0]} {outside[1]})
        (property "Reference" "P2" (at 0 0) (layer "F.SilkS"))
        (pad "1" smd circle (at 0 0) (size 0.2 0.2)
          (layers "F.Cu") (net 2 "B") (uuid "{outside_id}")))
      (gr_rect (start 0 0) (end 40 40) (stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts")))''')
    report = tmp_path / "native.json"
    result = subprocess.run(
        [cli, "pcb", "drc", "--format", "json", "--output", str(report), str(pcb)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    findings = json.loads(report.read_text())["violations"]
    collision_ids = {
        item["uuid"]
        for finding in findings
        if finding["type"] in ("clearance", "shorting_items")
        for item in finding["items"]
    }
    assert inside_id in collision_ids, findings
    assert outside_id not in collision_ids, findings

"""Analytic controls for the independent native-export comparison helper."""

import math
from pathlib import Path

import pytest

from tests._mask_gerbonara_oracle import read_native_gerber

pytest.importorskip("gerbonara", minversion="1.6.3")


def _gerber(tmp_path, commands):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "control.gbr"
    path.write_text("%FSLAX46Y46*%\n%MOMM*%\n%LPD*%\n" + commands + "\nM02*\n")
    return path


def test_oracle_rotated_rectangle_preserves_area_and_corners(tmp_path):
    # Gerbonara 1.6.3's generic rectangle-to-polygon method used its AABB.
    from shapely.geometry import Point

    path = _gerber(
        tmp_path,
        "%AMROT*21,1,4,1,0,0,30*%\n%ADD10ROT*%\nD10*\nX0Y0D03*",
    )
    shape = read_native_gerber(path)
    assert shape.area == pytest.approx(4, abs=1e-8)
    # At positive Gerber y, the board-frame y coordinate is negative.
    corner = Point(math.sqrt(3) - 0.25, -(1 + math.sqrt(3) / 4))
    assert shape.distance(corner) < 1e-8
    assert not shape.covers(Point(1.9, 1.2))


def test_aperture_hole_preserves_previously_drawn_copper(tmp_path):
    from shapely.geometry import Point

    path = _gerber(
        tmp_path,
        "%ADD10C,0.4*%\n%ADD11C,2X1*%\nD10*\nX0Y0D03*\nD11*\nX0Y0D03*",
    )
    shape = read_native_gerber(path)
    assert shape.covers(Point(0, 0))
    assert not shape.covers(Point(0.3, 0))
    assert shape.covers(Point(0.75, 0))
    assert shape.area == pytest.approx(math.pi * (1 - 0.25 + 0.04), abs=1e-5)


def test_clear_layer_object_erases_prior_copper(tmp_path):
    from shapely.geometry import Point

    path = _gerber(
        tmp_path,
        "%ADD10C,2*%\n%ADD11C,1*%\nD10*\nX0Y0D03*\n%LPC*%\nD11*\nX0Y0D03*",
    )
    shape = read_native_gerber(path)
    assert not shape.covers(Point(0, 0))
    assert shape.covers(Point(0.75, 0))
    assert shape.area == pytest.approx(math.pi * 0.75, abs=1e-5)


def test_circle_and_obround_flash_areas_match_analytic_shapes(tmp_path):
    from shapely.geometry import Point

    circle = _gerber(tmp_path / "circle", "%ADD10C,1*%\nD10*\nX0Y0D03*")
    assert read_native_gerber(circle).area == pytest.approx(math.pi * 0.25, abs=1e-5)

    stadium = math.pi * 0.25 + 0.5
    obround = _gerber(tmp_path / "obround", "%ADD10O,1.5X1*%\nD10*\nX0Y0D03*")
    shape = read_native_gerber(obround)
    assert shape.area == pytest.approx(stadium, abs=1e-5)
    assert shape.covers(Point(-0.7, 0))
    assert not shape.covers(Point(0, 0.76))

    # A rotated stadium: the same round pen drawn along a 30-degree line.
    rotated = _gerber(tmp_path / "rotated", "%ADD10C,1*%\nD10*\nX0Y0D02*\nX433013Y250000D01*")
    assert read_native_gerber(rotated).area == pytest.approx(stadium, abs=1e-5)


def test_zero_length_round_pen_renders_disk_not_dropped(tmp_path):
    # KiCad 10.0.6 plots round pads -- an obround with equal width and
    # height carries no straight segment -- as a zero-length line with a
    # width. Gerbonara 1.6.3 reports that as zero-size, so the oracle used
    # to drop the whole pad or opening and native comparisons could pass
    # vacuously (issue #9232). Both sources must render the full disk.
    from shapely.geometry import Point

    drawn = _gerber(tmp_path / "drawn", "%ADD10C,1.7*%\nD10*\nX0Y0D02*\nX0Y0D01*")
    assert read_native_gerber(drawn).area == pytest.approx(math.pi * 0.85**2, abs=1e-5)

    flashed = _gerber(tmp_path / "flashed", "%ADD10O,1.7X1.7*%\nD10*\nX0Y0D03*")
    shape = read_native_gerber(flashed)
    assert shape.area == pytest.approx(math.pi * 0.85**2, abs=1e-5)
    assert shape.covers(Point(0, 0))
    assert not shape.covers(Point(0.86, 0))


def test_zero_width_round_pen_is_still_skipped(tmp_path):
    path = _gerber(tmp_path, "%ADD10C,0*%\nD10*\nX0Y0D02*\nX1000000Y0D01*")
    assert read_native_gerber(path).is_empty


def test_clear_zero_length_flash_erases_dark_disk(tmp_path):
    path = _gerber(
        tmp_path,
        "%ADD10C,1.7*%\nD10*\nX0Y0D02*\nX0Y0D01*\n%LPC*%\nX0Y0D02*\nX0Y0D01*",
    )
    assert read_native_gerber(path).is_empty


def test_oracle_repairs_self_touching_region_like_production_reader():
    # Issue #8947: a KiCad 10 zone-fill contour that pinches off at a point
    # is invalid to Shapely but legal Gerber. The oracle must apply the same
    # repair as the production reader instead of rejecting the layer, so the
    # comparison stays an independent witness on real fill output.
    from kicad_tools.validate.gerber_geometry import parse_gerber_geometry

    path = (
        Path(__file__).parent
        / "fixtures"
        / "gerber_kicad10_region_aperture"
        / "kicad10_simple_led_zone_fill_B_Cu.gbl"
    )
    shape = read_native_gerber(path)
    reference = parse_gerber_geometry(path.read_text()).geometry
    assert shape.area > 0
    assert abs(shape.area - reference.area) <= 1e-9 * max(reference.area, 1.0)

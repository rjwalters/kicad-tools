"""Analytic controls for the independent native-export comparison helper."""

import math

import pytest

from tests._mask_gerbonara_oracle import read_native_gerber


def _gerber(tmp_path, commands):
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

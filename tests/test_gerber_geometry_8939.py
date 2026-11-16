"""Issue #8947: zero-size apertures are rejected on use, not on definition,
and self-touching KiCad 10 zone-fill region contours are repaired instead of
rejecting the layer. Both fixtures come from (or model) KiCad 10 output; see
tests/fixtures/gerber_kicad10_region_aperture/README.md for provenance.
"""

from pathlib import Path

import pytest

from kicad_tools.validate.gerber_geometry import GerberGeometryError, parse_gerber_geometry

HEADER = "%FSLAX46Y46*%\n%MOMM*%\n%LPD*%\n"
FIXTURES = Path(__file__).parent / "fixtures" / "gerber_kicad10_region_aperture"

ZERO_C_REGION = "%ADD10C,0.000000*%\nD10*\n" + (
    "G36*X0Y0D02*X1000000Y0D01*X1000000Y1000000D01*X0Y1000000D01*X0Y0D01*G37*"
)
DUMBBELL = (
    "G36*X0Y0D02*X1000000Y0D01*X1000000Y1000000D01*X2000000Y1000000D01*"
    "X2000000Y2000000D01*X1000000Y2000000D01*X1000000Y1000000D01*"
    "X0Y1000000D01*X0Y0D01*"
)
SQUARE_AT_5 = (
    "X5000000Y5000000D02*X6000000Y5000000D01*X6000000Y6000000D01*"
    "X5000000Y6000000D01*X5000000Y5000000D01*G37*"
)
BOW_TIE = "G36*X0Y0D02*X1000000Y1000000D01*X1000000Y0D01*X0Y1000000D01*X0Y0D01*G37*"


def test_zero_size_aperture_region_mode_fixture_parses():
    data = (FIXTURES / "kicad10_zero_size_aperture_region_mode.gbr").read_text()
    geometry = parse_gerber_geometry(data).geometry
    assert geometry.area == pytest.approx(1.0)


def test_kicad10_zone_fill_region_fixture_parses():
    data = (FIXTURES / "kicad10_simple_led_zone_fill_B_Cu.gbl").read_text()
    geometry = parse_gerber_geometry(data).geometry
    assert geometry.area == pytest.approx(457.199286)


def test_region_fixture_area_matches_independent_oracle():
    pytest.importorskip("gerbonara", minversion="1.6.3")
    from tests._mask_gerbonara_oracle import read_native_gerber

    path = FIXTURES / "kicad10_simple_led_zone_fill_B_Cu.gbl"
    geometry = parse_gerber_geometry(path.read_text()).geometry
    reference = read_native_gerber(path)
    assert geometry.area > 0
    assert abs(geometry.area - reference.area) <= 1e-9 * max(reference.area, 1.0)


@pytest.mark.parametrize(
    "aperture",
    [
        "%ADD10C,0.000000*%",
        "%ADD10C,0.000000X0.500000*%",  # zero size with a nonzero hole
        "%ADD10R,0X0*%",
        "%ADD10R,1X0*%",  # zero height only
        "%ADD10R,0X1*%",  # zero width only
        "%ADD10O,0X1*%",
        "%ADD10O,1X0*%",
        "%ADD10P,0X3X0*%",
    ],
)
def test_zero_size_aperture_flashed_is_rejected(aperture):
    with pytest.raises(GerberGeometryError, match="Nonpositive aperture"):
        parse_gerber_geometry(HEADER + aperture + "\nD10*\nX0Y0D03*\nM02*")


@pytest.mark.parametrize("aperture", ["%ADD10C,0.000000*%", "%ADD10R,0X0*%", "%ADD10O,0X1*%"])
def test_zero_size_aperture_stroked_is_rejected(aperture):
    with pytest.raises(GerberGeometryError, match="Nonpositive aperture"):
        parse_gerber_geometry(HEADER + aperture + "\nD10*\nX0Y0D02*\nX1000000Y0D01*\nM02*")


def test_zero_size_aperture_definition_and_selection_stay_legal():
    # The aperture that would raise on flash or stroke keeps region mode
    # working: definition, selection, and region-only use are all legal.
    assert parse_gerber_geometry(HEADER + ZERO_C_REGION + "M02*").geometry.area == pytest.approx(
        1.0
    )


def test_self_touching_contour_repairs_to_two_lobes():
    # Two 1 mm^2 lobes touching at (1,1): Shapely calls the ring invalid,
    # make_valid keeps both lobes, and the shoelace area survives the repair.
    geometry = parse_gerber_geometry(HEADER + DUMBBELL + "G37*" + "M02*").geometry
    assert geometry.area == pytest.approx(2.0)


def test_self_touching_contour_and_disjoint_contour_symmetric_difference():
    geometry = parse_gerber_geometry(HEADER + DUMBBELL + SQUARE_AT_5 + "M02*").geometry
    assert geometry.area == pytest.approx(3.0)


def test_self_intersecting_bow_tie_is_still_rejected():
    with pytest.raises(GerberGeometryError, match="Invalid region contour"):
        parse_gerber_geometry(HEADER + BOW_TIE + "M02*")


def test_open_region_contour_is_still_rejected():
    with pytest.raises(GerberGeometryError, match="Open or degenerate region"):
        parse_gerber_geometry(HEADER + "G36*X0Y0D02*X1000000Y0D01*X1000000Y1000000D01*G37*M02*")

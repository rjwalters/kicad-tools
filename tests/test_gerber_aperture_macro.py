"""Aperture-macro expansion for KiCad 10's %AMRoundRect profile (issue #8959).

KiCad 10 emits ``%AMRoundRect`` on every board's top copper and on most solder
mask layers; the reader used to reject the complete layer. These tests pin the
narrow expander: the committed archives parse where they should, the flashed
pad geometry matches the independent gerbonara oracle, and any macro construct
outside the supported subset still rejects the layer.
"""

from __future__ import annotations

import math
import re
import zipfile
from pathlib import Path

import pytest

from kicad_tools.validate.gerber_geometry import GerberGeometryError, parse_gerber_geometry

BOARDS = Path(__file__).resolve().parent.parent / "boards"

HEADER = "%FSLAX46Y46*%\n%MOMM*%\n%LPD*%\n"

# KiCad 10's %AMRoundRect body, byte-identical on every layer in the committed
# archives (issue #8959's verified census): comment, outline, 4 circles,
# 4 vector lines, with $1+$1 the only arithmetic form present.
KICAD_ROUNDRECT = """%AMRoundRect*
0 Rectangle with rounded corners*
0 $1 Rounding radius*
0 $2 $3 $4 $5 $6 $7 $8 $9 X,Y pos of 4 corners*
0 Add a 4 corners polygon primitive as box body*
4,1,4,$2,$3,$4,$5,$6,$7,$8,$9,$2,$3,0*
0 Add four circle primitives for the rounded corners*
1,1,$1+$1,$2,$3*
1,1,$1+$1,$4,$5*
1,1,$1+$1,$6,$7*
1,1,$1+$1,$8,$9*
0 Add four rect primitives between the rounded corners*
20,1,$1+$1,$2,$3,$4,$5,0*
20,1,$1+$1,$4,$5,$6,$7,0*
20,1,$1+$1,$6,$7,$8,$9,0*
20,1,$1+$1,$8,$9,$2,$3,0*%
"""

# The 02-charlieplex-led F_Cu instantiation: an inner 0.5 x 0.95 mm rectangle
# ($2..$9) grown by $1 = 0.25 mm into the flashed rounded pad, plus the
# unreferenced trailing rotation parameter KiCad always passes.
PAD_ADD = "%ADD10RoundRect,0.250000X-0.250000X-0.475000X0.250000X-0.475000X0.250000X0.475000X-0.250000X0.475000X0*%"


def _committed_layer(board: str, suffix: str) -> str:
    archive = BOARDS / board / "output" / "manufacturing" / "gerbers" / "gerbers.zip"
    with zipfile.ZipFile(archive) as zf:
        name = next(n for n in zf.namelist() if n.endswith(suffix))
        return zf.read(name).decode()


def _flash_projection(text: str) -> str:
    """Reduce a layer to its header, the macro, and only the macro flashes.

    Both the production reader and the gerbonara oracle then consume the same
    real KiCad-emitted commands, so the comparison isolates macro expansion
    from the pre-existing flash/pad disagreements measured at layer level.
    """
    macro = re.search(r"%AM\S+\*.*?\*%", text, re.DOTALL).group(0)
    adds = re.findall(r"%ADD\d+RoundRect,[^*]+\*%", text)
    macro_codes = {int(m.group(1)) for a in adds if (m := re.match(r"ADD(\d+)", a[1:]))}
    header = "".join(re.findall(r"%FSLAX[^%]*%|%MOMM\*%|%LPD\*%", text))
    lines = [header, macro, *adds]
    selected = None
    for line in text.splitlines():
        line = line.strip()
        if match := re.fullmatch(r"D(\d+)\*", line):
            selected = int(match[1])
        elif match := re.fullmatch(r"X-?\d+Y-?\d+D03\*", line):
            if selected in macro_codes:
                lines.extend((f"D{selected}*", match[0]))
    lines.append("M02*")
    return "\n".join(lines) + "\n"


def test_kicad_roundrect_flash_reproduces_the_rounded_pad():
    source = HEADER + KICAD_ROUNDRECT + PAD_ADD + "D10*X0Y0D03*X3000000Y0D03*M02*"
    image = parse_gerber_geometry(source).geometry
    # The flash is the 0.5 x 0.95 inner rectangle grown by r = 0.25 on all
    # sides: a 1.0 x 1.45 rounded rectangle with (4 - pi) r^2 corner loss.
    pad = (0.5 + 0.5) * (0.95 + 0.5) - (4 - math.pi) * 0.25**2
    assert image.area == pytest.approx(2 * pad, abs=1e-4)
    assert image.bounds == pytest.approx((-0.5, -0.725, 3.5, 0.725), abs=1e-4)


def test_macro_defined_but_never_flashed_contributes_nothing():
    plain = HEADER + "%ADD10C,1*%D10*X0Y0D03*M02*"
    with_macro = HEADER + KICAD_ROUNDRECT + PAD_ADD + "%ADD11C,1*%D11*X0Y0D03*M02*"
    assert parse_gerber_geometry(with_macro).geometry.equals(parse_gerber_geometry(plain).geometry)


def test_macro_exposure_zero_primitive_erases():
    source = (
        HEADER + "%AMBox*4,1,4,0,0,1,0,1,1,0,1,0,0*1,0,0.2,0.5,0.5*%%ADD10Box*%D10*X0Y0D03*M02*"
    )
    image = parse_gerber_geometry(source).geometry
    assert image.area == pytest.approx(1 - math.pi * 0.01, abs=1e-4)
    assert not image.contains(_point(0.5, 0.5))


def test_macro_aperture_scales_with_inch_units():
    # Under %MOIN the macro circle must land in millimetres exactly like the
    # standard-aperture path three lines below it in the %ADD handler.
    inch = "%FSLAX46Y46*%\n%MOIN*%\n%LPD*%\n"
    standard = inch + "%ADD10C,0.1*%D10*X0Y0D03*M02*"
    macro = inch + "%AMCirc*1,1,0.1,0,0*%%ADD10Circ*%D10*X0Y0D03*M02*"
    expected = math.pi * (0.1 * 25.4 / 2) ** 2
    standard_image = parse_gerber_geometry(standard).geometry
    macro_image = parse_gerber_geometry(macro).geometry
    assert standard_image.area == pytest.approx(expected, rel=1e-5)
    assert macro_image.area == standard_image.area


def test_leading_clear_macro_primitive_erases_nothing():
    # The macro image starts empty, so a clear-first primitive adds no copper
    # (the dark-then-clear order is pinned by test_macro_exposure_zero_primitive_erases).
    source = (
        HEADER
        + "%AMLC2*1,0,0.4,3,3*4,1,4,-0.5,-0.5,0.5,-0.5,0.5,0.5,-0.5,0.5,-0.5,-0.5,0*%"
        + "%ADD10LC2*%D10*X0Y0D03*M02*"
    )
    image = parse_gerber_geometry(source).geometry
    assert image.area == pytest.approx(1.0, abs=1e-4)
    assert image.bounds == pytest.approx((-0.5, -0.5, 0.5, 0.5), abs=1e-4)


def test_draw_with_macro_aperture_rejects():
    # Macro apertures are flashed only: the D01 sweep's convex hull is exact
    # for the convex C/R/O/P templates but over-reports non-convex macro shapes.
    source = (
        HEADER
        + "%AMLC2*4,1,4,-0.5,-0.5,0.5,-0.5,0.5,0.5,-0.5,0.5,-0.5,-0.5,0*%"
        + "%ADD10LC2*%D10*X0Y0D02*X2000000Y0D01*M02*"
    )
    with pytest.raises(GerberGeometryError, match="Draw with macro aperture: LC2"):
        parse_gerber_geometry(source)


def _point(x: float, y: float):
    from shapely.geometry import Point

    return Point(x, y)


@pytest.mark.parametrize(
    "body",
    [
        "%AMBad*5,1,1,0,0,0*%",
        "%AMBad*21,1,1,1,0,0,0*%",
        "%AMBad*6,1,1,0,0,0.5,0.5,0.1*%",
        "%AMBad*7,1,1,0,0,0.5,0.5,0.1*%",
    ],
)
def test_unsupported_macro_primitive_rejects_naming_macro_and_code(body):
    with pytest.raises(
        GerberGeometryError, match=r"Unsupported macro primitive (5|21|6|7) in macro Bad"
    ):
        parse_gerber_geometry(HEADER + body + "%ADD10C,1*%D10*X0Y0D03*M02*")


@pytest.mark.parametrize(
    "field",
    ["$1-$1", "$1x$1", "2x$1", "$1+$1+$1", "($1)", "$1.5"],
)
def test_macro_arithmetic_outside_a_single_add_rejects(field):
    body = f"%AMBad*1,1,{field},0,0*%"
    with pytest.raises(GerberGeometryError, match="Unsupported arithmetic in macro Bad"):
        parse_gerber_geometry(HEADER + body + "%ADD10Bad,1*%D10*X0Y0D03*M02*")


def test_unbound_macro_parameter_rejects():
    body = "%AMBad*1,1,$10,0,0*%"
    with pytest.raises(GerberGeometryError, match="Unbound macro parameter in macro Bad: \\$10"):
        parse_gerber_geometry(HEADER + body + "%ADD10Bad,1*%D10*X0Y0D03*M02*")


def test_macro_defined_after_use_rejects():
    source = HEADER + "%ADD10RoundRect,1X1X1X1X1X1X1X1X1*%" + KICAD_ROUNDRECT + "D10*X0Y0D03*M02*"
    with pytest.raises(GerberGeometryError, match="Unknown aperture template: RoundRect"):
        parse_gerber_geometry(source)


REGION_FREE_MACRO_COPPER = [
    ("02-charlieplex-led", "-F_Cu.gtl"),
    ("02-charlieplex-led", "-B_Cu.gbl"),
    ("06-diffpair-test", "-F_Cu.gtl"),
    ("07-matchgroup-test", "-F_Cu.gtl"),
    ("07-matchgroup-test", "-B_Cu.gbl"),
]

REGION_CARRYING_MACRO_COPPER = [
    ("00-simple-led", "-F_Cu.gtl"),
    ("01-voltage-divider", "-F_Cu.gtl"),
    ("03-usb-joystick", "-F_Cu.gtl"),
    ("04-stm32-devboard", "-F_Cu.gtl"),
    ("05-bldc-motor-controller", "-F_Cu.gtl"),
]


@pytest.mark.parametrize(("board", "suffix"), REGION_FREE_MACRO_COPPER)
def test_region_free_macro_copper_layers_parse(board, suffix):
    image = parse_gerber_geometry(_committed_layer(board, suffix)).geometry
    assert image.area > 0


@pytest.mark.parametrize(("board", "suffix"), REGION_FREE_MACRO_COPPER)
def test_committed_macro_flashes_match_independent_gerbonara(board, suffix, tmp_path):
    pytest.importorskip("gerbonara", minversion="1.6.3")
    from tests._mask_gerbonara_oracle import read_native_gerber

    projection = tmp_path / f"{board}{suffix}.gbr"
    projection.write_text(_flash_projection(_committed_layer(board, suffix)))
    ours = parse_gerber_geometry(projection.read_text()).geometry
    reference = read_native_gerber(projection)
    assert ours.area > 0 and reference.area > 0
    assert ours.difference(reference.buffer(0.000005)).is_empty
    assert reference.difference(ours.buffer(0.000005)).is_empty
    assert ours.symmetric_difference(reference).area <= 0.000005 * reference.length


def test_all_committed_solder_mask_layers_parse():
    layers = []
    for archive in sorted(BOARDS.glob("*/output/manufacturing/gerbers/gerbers.zip")):
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                if name.endswith(("-F_Mask.gts", "-B_Mask.gbs")):
                    layers.append(zf.read(name).decode())
    assert len(layers) == 16
    for data in layers:
        assert parse_gerber_geometry(data).geometry is not None


@pytest.mark.parametrize(("board", "suffix"), REGION_CARRYING_MACRO_COPPER)
def test_region_carrying_macro_copper_layers_parse_after_region_repair(board, suffix):
    # These layers carry self-touching region contours alongside macro flashes.
    # The macro subset here parses past the aperture stage and main's #8947
    # region repair resolves the contours, so the full layer now parses.
    image = parse_gerber_geometry(_committed_layer(board, suffix)).geometry
    assert image.area > 0


def test_macro_primitive_rotation_matches_gerbonara(tmp_path):
    pytest.importorskip("gerbonara", minversion="1.6.3")
    from tests._mask_gerbonara_oracle import read_native_gerber

    rotated = (
        KICAD_ROUNDRECT.replace("$8,$9,$2,$3,0*", "$8,$9,$2,$3,30*")
        .replace("1,1,$1+$1,$2,$3*", "1,1,$1+$1,$2,$3,30*")
        .replace("1,1,$1+$1,$4,$5*", "1,1,$1+$1,$4,$5,30*")
        .replace("1,1,$1+$1,$6,$7*", "1,1,$1+$1,$6,$7,30*")
        .replace("1,1,$1+$1,$8,$9*", "1,1,$1+$1,$8,$9,30*")
        .replace(",$4,$5,0*", ",$4,$5,30*")
        .replace(",$6,$7,0*", ",$6,$7,30*")
        .replace(",$8,$9,0*", ",$8,$9,30*")
        .replace(",$2,$3,0*", ",$2,$3,30*")
    )
    assert rotated != KICAD_ROUNDRECT
    source = HEADER + rotated + PAD_ADD + "D10*X5000000Y5000000D03*M02*"
    projection = tmp_path / "rotated.gbr"
    projection.write_text(source)
    ours = parse_gerber_geometry(source).geometry
    reference = read_native_gerber(projection)
    assert ours.difference(reference.buffer(0.000005)).is_empty
    assert reference.difference(ours.buffer(0.000005)).is_empty
    assert ours.symmetric_difference(reference).area <= 0.000005 * reference.length

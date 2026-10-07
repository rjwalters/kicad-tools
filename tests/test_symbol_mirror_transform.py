"""Placed-symbol ``(mirror x|y)`` handling (issue #6005).

KiCad rotates a placed symbol first, then mirrors: ``(mirror x)`` negates the
rotated library Y, ``(mirror y)`` the rotated library X.  The expected offsets
below were checked against ``kicad-cli sch export netlist`` (KiCad 10.0.1) by
placing a label at each predicted pin position for all 12 rotation x mirror
combinations of an asymmetric symbol: every pin joined its label's net.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.core.symbol_transform import normalize_mirror, symbol_to_sheet_offset
from kicad_tools.schema.library import LibraryPin, LibrarySymbol
from kicad_tools.schematic.models import Schematic
from kicad_tools.schematic.models.elements import PowerSymbol
from kicad_tools.sexp import parse_string

# Schematic (Y-down) offset from the symbol origin of a pin whose connection
# point is at library (-7.62, 2.54).  Same table as
# tests/test_pad_pintype_annotation.py::_PIN1_OFFSET (PR #6000).
PIN1_OFFSET = {
    (0, ""): (-7.62, -2.54),
    (0, "x"): (-7.62, 2.54),
    (0, "y"): (7.62, -2.54),
    (90, ""): (-2.54, 7.62),
    (90, "x"): (-2.54, -7.62),
    (90, "y"): (2.54, 7.62),
    (180, ""): (7.62, 2.54),
    (180, "x"): (7.62, -2.54),
    (180, "y"): (-7.62, 2.54),
    (270, ""): (2.54, -7.62),
    (270, "x"): (2.54, 7.62),
    (270, "y"): (-2.54, -7.62),
}

ORIENTATIONS = sorted(PIN1_OFFSET)


def _pin(num: str, x: float, y: float, angle: int) -> str:
    return (
        f"(pin passive line (at {x} {y} {angle}) (length 2.54)"
        f' (name "P{num}" (effects (font (size 1.27 1.27))))'
        f' (number "{num}" (effects (font (size 1.27 1.27)))))'
    )


# Asymmetric: no pin is the mirror twin of another.
_LIB = (
    '(lib_symbols (symbol "T:ASYM" (in_bom yes) (on_board yes)'
    ' (property "Reference" "U" (at 0 10 0)) (property "Value" "ASYM" (at 0 -10 0))'
    f' (symbol "ASYM_1_1" {_pin("1", -7.62, 2.54, 0)} {_pin("3", 10.16, 1.27, 180)}'
    f" {_pin('4', 2.54, 7.62, 270)})))"
)


def _sheet(path: Path, placements: list[tuple[str, int, str]]) -> Path:
    """Write a sheet placing ``T:ASYM`` at (100, 100) once per placement."""
    syms = []
    for i, (ref, rotation, mirror) in enumerate(placements):
        mir = f" (mirror {mirror})" if mirror else ""
        syms.append(
            f'(symbol (lib_id "T:ASYM") (at 100 100 {rotation}){mir} (unit 1)'
            f' (in_bom yes) (on_board yes) (uuid "00000000-0000-0000-0000-0000000002{i:02d}")'
            f' (property "Reference" "{ref}" (at 0 0 0)) (property "Value" "ASYM" (at 0 0 0)))'
        )
    path.write_text(
        '(kicad_sch (version 20250114) (generator "test")'
        ' (uuid "00000000-0000-0000-0000-000000000100") (paper "A3")\n'
        f"{_LIB}\n" + "\n".join(syms) + "\n)\n"
    )
    return path


def _ref(rotation: int, mirror: str) -> str:
    return f"U{rotation}{mirror or 'n'}"


@pytest.fixture
def placed(tmp_path: Path) -> dict[str, object]:
    sheet = _sheet(tmp_path / "m.kicad_sch", [(_ref(r, m), r, m) for r, m in ORIENTATIONS])
    return {s.reference: s for s in Schematic.load(str(sheet)).symbols}


@pytest.mark.parametrize(("rotation", "mirror"), ORIENTATIONS)
def test_shared_transform_matches_kicad(rotation: int, mirror: str) -> None:
    dx, dy = symbol_to_sheet_offset(-7.62, 2.54, rotation, mirror)
    assert (round(dx, 2), round(dy, 2)) == PIN1_OFFSET[(rotation, mirror)]


@pytest.mark.parametrize(("rotation", "mirror"), ORIENTATIONS)
def test_model_pin_position_applies_mirror(
    placed: dict[str, object], rotation: int, mirror: str
) -> None:
    sym = placed[_ref(rotation, mirror)]
    assert sym.mirror == mirror
    dx, dy = PIN1_OFFSET[(rotation, mirror)]
    assert sym.pin_position("1") == (round(100 + dx, 2), round(100 + dy, 2))


@pytest.mark.parametrize(
    ("rotation", "mirror", "expected"),
    [
        # The four combinations the acceptance criteria name explicitly,
        # on pin 3 at library (10.16, 1.27).
        (0, "x", (110.16, 101.27)),
        (0, "y", (89.84, 98.73)),
        (90, "x", (98.73, 110.16)),
        (90, "y", (101.27, 89.84)),
    ],
)
def test_model_mirror_x_and_y_at_0_and_90(
    placed: dict[str, object], rotation: int, mirror: str, expected: tuple[float, float]
) -> None:
    assert placed[_ref(rotation, mirror)].pin_position("3") == expected


@pytest.mark.parametrize(("rotation", "mirror"), ORIENTATIONS)
def test_library_get_pin_position_agrees_with_model(
    placed: dict[str, object], rotation: int, mirror: str
) -> None:
    lib = LibrarySymbol(
        name="ASYM",
        pins=[
            LibraryPin(
                number="1",
                name="P1",
                type="passive",
                position=(-7.62, 2.54),
                rotation=0,
                length=2.54,
            ),
            LibraryPin(
                number="3",
                name="P3",
                type="passive",
                position=(10.16, 1.27),
                rotation=0,
                length=2.54,
            ),
            LibraryPin(
                number="4",
                name="P4",
                type="passive",
                position=(2.54, 7.62),
                rotation=0,
                length=2.54,
            ),
        ],
    )
    sym = placed[_ref(rotation, mirror)]
    for num in ("1", "3", "4"):
        lib_xy = lib.get_pin_position(num, (100, 100), rotation, mirror)
        assert lib_xy == pytest.approx(sym.pin_position(num), abs=1e-6)


@pytest.mark.parametrize(("rotation", "mirror"), ORIENTATIONS)
def test_mirror_round_trips_through_write(tmp_path: Path, rotation: int, mirror: str) -> None:
    src = _sheet(tmp_path / "a.kicad_sch", [("U1", rotation, mirror)])
    out = tmp_path / "b.kicad_sch"
    Schematic.load(str(src)).write(str(out), auto_size_paper=False)
    text = out.read_text()
    assert ("(mirror " in text) == bool(mirror)
    reloaded = Schematic.load(str(out)).symbols[0]
    assert reloaded.mirror == mirror
    dx, dy = PIN1_OFFSET[(rotation, mirror)]
    assert reloaded.pin_position("1") == (round(100 + dx, 2), round(100 + dy, 2))


def test_string_serializer_emits_mirror(placed: dict[str, object]) -> None:
    assert "(mirror x)" in placed[_ref(90, "x")].to_sexp("p", "/")
    assert "(mirror" not in placed[_ref(90, "")].to_sexp("p", "/")


@pytest.mark.parametrize(
    ("rotation", "mirror", "expected"),
    [
        # Pin 1 points right into the body, so its stub goes left unmirrored.
        (0, "", "left"),
        (0, "x", "left"),
        (0, "y", "right"),
        (90, "", "down"),
        (90, "x", "up"),
        (90, "y", "down"),
    ],
)
def test_outward_stub_direction_applies_mirror(
    placed: dict[str, object], rotation: int, mirror: str, expected: str
) -> None:
    sch = Schematic(title="t")
    assert sch._pin_outward_direction(placed[_ref(rotation, mirror)], "1") == expected


def test_power_symbol_mirror_round_trips() -> None:
    node = parse_string(
        '(symbol (lib_id "power:GND") (at 10 20 0) (mirror x) (unit 1)'
        ' (uuid "00000000-0000-0000-0000-000000000001")'
        ' (property "Reference" "#PWR01" (at 0 0 0)) (property "Value" "GND" (at 0 0 0)))'
    )
    pwr = PowerSymbol.from_sexp(node)
    assert pwr.mirror == "x"
    assert "(mirror x)" in pwr.to_sexp("p", "/")
    assert "(mirror" not in PowerSymbol(lib_id="power:GND", x=0, y=0).to_sexp("p", "/")


@pytest.mark.parametrize(("raw", "expected"), [("x", "x"), ("Y", "y"), ("", ""), ("z", "")])
def test_normalize_mirror(raw: str, expected: str) -> None:
    assert normalize_mirror(raw) == expected


_STM32_TEMPLATE = Path(
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/template/"
    "stm32f100-discovery-shield/stm32f100-discovery-shield.kicad_sch"
)


@pytest.mark.skipif(not _STM32_TEMPLATE.exists(), reason="KiCad stm32f100 template not installed")
def test_stm32f100_template_p2_pin27_lands_on_its_no_connect() -> None:
    """The issue's reproduction: P2 is ``(at 271.78 124.46 0) (mirror x)``."""
    sch = Schematic.load(str(_STM32_TEMPLATE))
    p2 = next(s for s in sch.symbols if s.reference == "P2")
    assert p2.mirror == "x"
    assert p2.pin_position("27") == (266.7, 91.44)
    assert p2.pin_position("1") == (266.7, 157.48)

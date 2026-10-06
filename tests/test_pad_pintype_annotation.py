"""Schematic pin types copied onto PCB pads (issue #5985).

KiCad's "Update PCB from Schematic" writes ``(pinfunction ...)`` and
``(pintype ...)`` on every pad.  kct's board generators build PCBs directly,
so before #5985 they carried neither, and the pin-type rail evidence in
:func:`kicad_tools.explain.mistakes.power_pin_nets` (issue #5939) never ran
on the project's own boards.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kicad_tools.explain.mistakes import is_power_net, power_pin_nets
from kicad_tools.operations.pintype import (
    annotate_pcb_file_pintypes,
    annotate_pcb_pintypes,
    schematic_pin_info,
)
from kicad_tools.schema.pcb import PCB

REPO_ROOT = Path(__file__).resolve().parents[1]

# A regulator whose rails have names the heuristic cannot classify (``MAIN``,
# ``VREG_1V2``), a resistor with unnamed (``~``) pins, and one unconnected pin.
_SCHEMATIC = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Test:REG" (in_bom yes) (on_board yes)
      (property "Reference" "U" (at 0 0 0))
      (property "Value" "REG" (at 0 0 0))
      (symbol "REG_1_1"
        (pin power_in line (at -7.62 2.54 0) (length 2.54) (name "VIN" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin power_in line (at 0 -7.62 90) (length 2.54) (name "GND" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
        (pin power_out line (at 7.62 2.54 180) (length 2.54) (name "VOUT" (effects (font (size 1.27 1.27)))) (number "3" (effects (font (size 1.27 1.27)))))
        (pin input line (at -7.62 0 0) (length 2.54) (name "EN" (effects (font (size 1.27 1.27)))) (number "4" (effects (font (size 1.27 1.27)))))
        (pin no_connect line (at 7.62 0 180) (length 2.54) (name "NC" (effects (font (size 1.27 1.27)))) (number "5" (effects (font (size 1.27 1.27)))))))
    (symbol "Device:R" (pin_numbers hide) (pin_names (offset 0)) (in_bom yes) (on_board yes)
      (property "Reference" "R" (at 2.032 0 90))
      (property "Value" "R" (at 0 0 90))
      (symbol "R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27))))))))
  (symbol (lib_id "Test:REG") (at 100 50 0) (unit 1)
    (in_bom yes) (on_board yes)
    (uuid "00000000-0000-0000-0000-000000000010")
    (property "Reference" "U1" (at 100 45 0))
    (property "Value" "REG" (at 100 55 0))
    (property "Footprint" "Package_TO_SOT_SMD:SOT-23-5" (at 0 0 0))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000011"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000012"))
    (pin "3" (uuid "00000000-0000-0000-0000-000000000013"))
    (pin "4" (uuid "00000000-0000-0000-0000-000000000014"))
    (pin "5" (uuid "00000000-0000-0000-0000-000000000015")))
  (symbol (lib_id "Device:R") (at 130 50 0) (unit 1)
    (in_bom yes) (on_board yes)
    (uuid "00000000-0000-0000-0000-000000000020")
    (property "Reference" "R1" (at 131 48 0))
    (property "Value" "10k" (at 131 51 0))
    (property "Footprint" "Resistor_SMD:R_0805" (at 0 0 0))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000021"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000022")))
)
"""

# Mixed layout on purpose: U1's pads are one child per line (KiCad style),
# R1's are single-line (kct hand-written generator style), H1 has no symbol.
_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "MAIN")
  (net 2 "GND")
  (net 3 "VREG_1V2")
  (net 4 "EN")
  (footprint "Package_TO_SOT_SMD:SOT-23-5"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "U1")
    (property "Value" "REG")
    (pad "1" smd rect
      (at -1 -1)
      (size 0.6 0.6)
      (layers "F.Cu")
      (net 1 "MAIN")
    )
    (pad "2" smd rect
      (at -1 0)
      (size 0.6 0.6)
      (layers "F.Cu")
      (net 2 "GND")
    )
    (pad "3" smd rect
      (at -1 1)
      (size 0.6 0.6)
      (layers "F.Cu")
      (net 3 "VREG_1V2")
    )
    (pad "4" smd rect
      (at 1 1)
      (size 0.6 0.6)
      (layers "F.Cu")
      (net 4 "EN")
    )
    (pad "5" smd rect
      (at 1 -1)
      (size 0.6 0.6)
      (layers "F.Cu")
    )
  )
  (footprint "Resistor_SMD:R_0805"
    (layer "F.Cu")
    (at 20 10)
    (property "Reference" "R1")
    (property "Value" "10k")
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 3 "VREG_1V2"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 4 "EN"))
  )
  (footprint "MountingHole:MountingHole_3.2mm"
    (layer "F.Cu")
    (at 30 10)
    (property "Reference" "H1")
    (pad "" np_thru_hole circle (at 0 0) (size 3.2 3.2) (drill 3.2) (layers "*.Cu"))
  )
)
"""

_EXPECTED = {
    ("U1", "1"): ("VIN", "power_in"),
    ("U1", "2"): ("GND", "power_in"),
    ("U1", "3"): ("VOUT", "power_out"),
    ("U1", "4"): ("EN", "input"),
    # Already ``no_connect``: no ``+no_connect`` suffix on top.
    ("U1", "5"): ("NC", "no_connect"),
    # Unnamed (``~``) pins get no pinfunction.
    ("R1", "1"): ("", "passive"),
    ("R1", "2"): ("", "passive"),
}


@pytest.fixture
def design(tmp_path: Path) -> tuple[Path, Path]:
    sch = tmp_path / "reg.kicad_sch"
    pcb = tmp_path / "reg.kicad_pcb"
    sch.write_text(_SCHEMATIC)
    pcb.write_text(_PCB)
    return sch, pcb


def _pad_children(pcb_path: Path) -> dict[tuple[str, str], tuple[str, str]]:
    """Read ``(pinfunction, pintype)`` per pad straight from the S-expression."""
    from kicad_tools.sexp import parse_file

    out: dict[tuple[str, str], tuple[str, str]] = {}
    doc = parse_file(pcb_path)
    for fp in doc.find_all("footprint"):
        ref = next(
            p.get_string(1) for p in fp.find_all("property") if p.get_string(0) == "Reference"
        )
        for pad in fp.find_all("pad"):
            func = pad.find_child("pinfunction")
            ptype = pad.find_child("pintype")
            if func is None and ptype is None:
                continue
            out[(ref, pad.get_string(0) or "")] = (
                func.get_string(0) if func is not None else "",
                ptype.get_string(0) if ptype is not None else "",
            )
    return out


def test_schematic_pin_info_reads_electrical_types(design: tuple[Path, Path]) -> None:
    sch, _ = design
    info = schematic_pin_info(sch)
    assert info[("U1", "1")].pintype == "power_in"
    assert info[("U1", "3")].pintype == "power_out"
    assert info[("U1", "3")].pinfunction == "VOUT"
    assert info[("R1", "1")].pinfunction == ""


def test_unannotated_board_has_no_pin_type_evidence(design: tuple[Path, Path]) -> None:
    """Baseline: without pintype, the unconventional rail names are invisible."""
    _, pcb = design
    assert power_pin_nets(PCB.load(pcb)) == set()
    assert not is_power_net("MAIN")
    assert not is_power_net("VREG_1V2")


def test_file_annotation_round_trip(design: tuple[Path, Path]) -> None:
    sch, pcb = design
    result = annotate_pcb_file_pintypes(pcb, sch)

    assert result.updated == len(_EXPECTED)
    assert result.missing_pads == []
    assert _pad_children(pcb) == _EXPECTED

    loaded = PCB.load(pcb)
    u1 = loaded.get_footprint("U1")
    assert u1 is not None
    assert {p.number: p.pintype for p in u1.pads}["3"] == "power_out"

    # The point of the issue: rails found by evidence, ground excluded.
    assert power_pin_nets(loaded) == {"MAIN", "VREG_1V2"}


def test_file_annotation_preserves_text_and_layout(design: tuple[Path, Path]) -> None:
    sch, pcb = design
    annotate_pcb_file_pintypes(pcb, sch)
    text = pcb.read_text()

    # Removing exactly the inserted children restores the original bytes.
    import re

    stripped = re.sub(r'\s*\((?:pinfunction|pintype) "[^"]*"\)', "", text)
    assert stripped == _PCB

    # Multi-line pads get one child per line at the net's indentation (as
    # KiCad writes them); single-line pads stay single-line.
    assert "\t" not in text
    assert '      (net 1 "MAIN")\n      (pinfunction "VIN")\n      (pintype "power_in")\n' in text
    assert '(net 3 "VREG_1V2") (pintype "passive"))' in text
    # Pad without a net: children go before the pad's closing paren.
    assert '(layers "F.Cu")\n      (pinfunction "NC")\n      (pintype "no_connect")\n    )' in text


def test_file_annotation_is_idempotent(design: tuple[Path, Path]) -> None:
    sch, pcb = design
    annotate_pcb_file_pintypes(pcb, sch)
    first = pcb.read_text()
    again = annotate_pcb_file_pintypes(pcb, sch)
    assert again.updated == 0
    assert again.unchanged == len(_EXPECTED)
    assert pcb.read_text() == first


def test_file_annotation_replaces_stale_values(design: tuple[Path, Path]) -> None:
    sch, pcb = design
    pcb.write_text(
        _PCB.replace(
            '(net 4 "EN"))',
            '(net 4 "EN") (pinfunction "OLD") (pintype "power_in"))',
        )
    )
    annotate_pcb_file_pintypes(pcb, sch)
    assert _pad_children(pcb)[("R1", "2")] == ("", "passive")
    assert "OLD" not in pcb.read_text()


# R1 sits at (130, 50) unrotated: pin 1 at library (0, 3.81) lands on
# (130, 46.19), pin 2 at library (0, -3.81) on (130, 53.81).
_NC_ON_R1_PIN2 = '  (no_connect (at 130 53.81) (uuid "00000000-0000-0000-0000-0000000000f2"))\n)\n'
_R1_PIN2_UNCONNECTED_NET = '(net 5 "unconnected-(R1-Pad2)"))'


def _flag_r1_pin2(sch: Path) -> None:
    """Put a schematic no-connect flag on R1 pin 2."""
    sch.write_text(_SCHEMATIC.rstrip().removesuffix(")") + _NC_ON_R1_PIN2)


def _r1_pin2_on_unconnected_net(pcb: Path) -> None:
    """Move R1 pad 2 onto KiCad's single-pad ``unconnected-(...)`` net."""
    text = _PCB.replace('  (net 4 "EN")\n', '  (net 4 "EN")\n  (net 5 "unconnected-(R1-Pad2)")\n')
    pcb.write_text(text.replace('(net 4 "EN"))', _R1_PIN2_UNCONNECTED_NET))


def test_schematic_no_connect_flag_sets_pin_info(design: tuple[Path, Path]) -> None:
    sch, _ = design
    _flag_r1_pin2(sch)
    info = schematic_pin_info(sch)
    assert info[("R1", "2")].no_connect
    assert info[("R1", "2")].effective_pintype == "passive+no_connect"
    assert not info[("R1", "1")].no_connect
    assert info[("R1", "1")].effective_pintype == "passive"


@pytest.mark.parametrize("pad_net", ["unconnected", "none"])
def test_flagged_pin_gets_no_connect_suffix(design: tuple[Path, Path], pad_net: str) -> None:
    """KiCad writes ``<type>+no_connect`` for a pin with a no-connect flag.

    It must survive whether the pad sits on KiCad's ``unconnected-(...)``
    net (KiCad-saved boards, and pads after ``sync-netlist``) or on no net
    at all (kct-generated boards): the suffix is schematic-derived.
    """
    sch, pcb = design
    _flag_r1_pin2(sch)
    if pad_net == "unconnected":
        _r1_pin2_on_unconnected_net(pcb)
    else:
        pcb.write_text(_PCB.replace(' (net 4 "EN"))', ")"))
    annotate_pcb_file_pintypes(pcb, sch)
    assert _pad_children(pcb)[("R1", "2")] == ("", "passive+no_connect")
    assert _pad_children(pcb)[("R1", "1")] == ("", "passive")


def test_unflagged_unconnected_net_keeps_plain_type(design: tuple[Path, Path]) -> None:
    """No flag, no suffix -- even on an ``unconnected-(...)`` net.

    KiCad 10's Edgeberry and STM32 Nucleo-64 templates have such pads typed
    plain ``passive``/``output``; re-annotating must not add the suffix.
    """
    sch, pcb = design
    _r1_pin2_on_unconnected_net(pcb)
    annotate_pcb_file_pintypes(pcb, sch)
    assert _pad_children(pcb)[("R1", "2")] == ("", "passive")


def test_pristine_kicad_no_connect_value_is_kept(design: tuple[Path, Path]) -> None:
    """Re-annotating KiCad's own ``passive+no_connect`` is a no-op (byte-identical)."""
    sch, pcb = design
    _flag_r1_pin2(sch)
    _r1_pin2_on_unconnected_net(pcb)
    annotate_pcb_file_pintypes(pcb, sch)
    annotated = pcb.read_text()
    again = annotate_pcb_file_pintypes(pcb, sch)
    assert again.updated == 0
    assert pcb.read_text() == annotated
    assert '"unconnected-(R1-Pad2)") (pintype "passive+no_connect"))' in annotated


def test_mirrored_symbol_flag_resolves_to_the_right_pin(design: tuple[Path, Path]) -> None:
    """``(mirror x)`` is applied before the flag is matched.

    Mirrored, R1 pin 2 lands where pin 1 would be unmirrored, so ignoring the
    mirror would put the suffix on the wrong pad (KiCad's
    stm32f100-discovery-shield template, P2).
    """
    sch, _ = design
    mirrored = _SCHEMATIC.replace(
        '(symbol (lib_id "Device:R") (at 130 50 0) (unit 1)',
        '(symbol (lib_id "Device:R") (at 130 50 0) (mirror x) (unit 1)',
    )
    nc = '  (no_connect (at 130 46.19) (uuid "00000000-0000-0000-0000-0000000000f3"))\n)\n'
    sch.write_text(mirrored.rstrip().removesuffix(")") + nc)
    info = schematic_pin_info(sch)
    assert info[("R1", "2")].no_connect
    assert not info[("R1", "1")].no_connect


# --- Placement geometry and connectivity, checked against KiCad ------------
#
# Every expectation below was confirmed with ``kicad-cli sch export netlist``
# (KiCad 10.0.1), which writes KiCad's own ``(pintype "...+no_connect")`` per
# node: the 12 rotation x mirror combinations, wire stubs, junctions and
# unit-0 pins of a multi-unit symbol (PR #6000 review).


def _asym_pin(num: str, x: float, y: float, ptype: str) -> str:
    return (
        f'(pin {ptype} line (at {x} {y} 0) (length 2.54) (name "P{num}" '
        f'(effects (font (size 1.27 1.27)))) (number "{num}" '
        f"(effects (font (size 1.27 1.27)))))"
    )


# Asymmetric two-unit symbol: no pin is the mirror twin of another, so a
# wrong transform lands on empty space or on a *different* pin.  Pin 5 is a
# unit-0 (common) pin drawn with every unit.
_ASYM_LIB = (
    '(lib_symbols (symbol "T:ASYM" (in_bom yes) (on_board yes)'
    ' (property "Reference" "U" (at 0 10 0)) (property "Value" "ASYM" (at 0 -10 0))'
    f' (symbol "ASYM_0_1" {_asym_pin("5", 0, -7.62, "power_in")})'
    f' (symbol "ASYM_1_1" {_asym_pin("1", -7.62, 2.54, "input")}'
    f" {_asym_pin('2', -7.62, 0, 'input')} {_asym_pin('3', 7.62, 1.27, 'output')})"
    f' (symbol "ASYM_2_1" {_asym_pin("4", -7.62, 3.81, "bidirectional")}'
    f" {_asym_pin('6', 7.62, -2.54, 'open_collector')})))"
)


def _asym_sheet(
    path: Path,
    *,
    rotation: int = 0,
    mirror: str = "",
    units: tuple[int, ...] = (1,),
    extra: str = "",
) -> Path:
    """Write a sheet with U1 (``T:ASYM``) placed at (100, 100) per unit."""
    placed = []
    for i, unit in enumerate(units):
        y = 100 + 40 * i
        mir = f" (mirror {mirror})" if mirror else ""
        placed.append(
            f'(symbol (lib_id "T:ASYM") (at 100 {y} {rotation}){mir} (unit {unit})'
            f' (in_bom yes) (on_board yes) (uuid "00000000-0000-0000-0000-0000000001{i:02d}")'
            ' (property "Reference" "U1" (at 0 0 0)) (property "Value" "ASYM" (at 0 0 0)))'
        )
    path.write_text(
        '(kicad_sch (version 20250114) (generator "test")'
        ' (uuid "00000000-0000-0000-0000-000000000100") (paper "A3")\n'
        f"{_ASYM_LIB}\n" + "\n".join(placed) + f"\n{extra}\n)\n"
    )
    return path


def _nc(x: float, y: float) -> str:
    return f"(no_connect (at {x} {y}))"


def _wire(x1: float, y1: float, x2: float, y2: float) -> str:
    return f"(wire (pts (xy {x1} {y1}) (xy {x2} {y2})))"


def _flagged(sch: Path) -> set[str]:
    return {pin for (ref, pin), v in schematic_pin_info(sch).items() if v.no_connect}


# Schematic (Y-down) offset of U1 pin 1 (library (-7.62, 2.54)) from the
# symbol origin.  KiCad rotates first, then mirrors.
_PIN1_OFFSET = {
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


@pytest.mark.parametrize(("rotation", "mirror"), sorted(_PIN1_OFFSET))
def test_flag_resolves_to_the_right_pin_at_every_orientation(
    tmp_path: Path, rotation: int, mirror: str
) -> None:
    """Rotation then mirror: at 90/270 degrees the order changes the answer."""
    dx, dy = _PIN1_OFFSET[(rotation, mirror)]
    sch = _asym_sheet(
        tmp_path / "u.kicad_sch", rotation=rotation, mirror=mirror, extra=_nc(100 + dx, 100 + dy)
    )
    assert _flagged(sch) == {"1"}


@pytest.mark.parametrize(("rotation", "mirror"), [(90, "x"), (90, "y"), (270, "x"), (270, "y")])
def test_mirror_before_rotation_position_is_not_the_pin(
    tmp_path: Path, rotation: int, mirror: str
) -> None:
    """The old mirror-then-rotate position of pin 1 is the opposite mirror's."""
    other = {"x": "y", "y": "x"}[mirror]
    dx, dy = _PIN1_OFFSET[(rotation, other)]
    sch = _asym_sheet(
        tmp_path / "u.kicad_sch", rotation=rotation, mirror=mirror, extra=_nc(100 + dx, 100 + dy)
    )
    assert _flagged(sch) == set()


# U1 unit 1 at (100, 100), rotation 0: pin 1 at (92.38, 97.46),
# pin 3 at (107.62, 98.73).
@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        # Flag at the far end of a stub wire from pin 3.
        (_wire(107.62, 98.73, 117.78, 98.73) + _nc(117.78, 98.73), {"3"}),
        # Two-segment chain joined end to end.
        (
            _wire(107.62, 98.73, 112.7, 98.73)
            + _wire(112.7, 98.73, 112.7, 108.89)
            + _nc(112.7, 108.89),
            {"3"},
        ),
        # Bare "T": a branch ending on the stub's interior does not connect...
        (
            _wire(107.62, 98.73, 117.78, 98.73)
            + _wire(112.7, 98.73, 112.7, 103.81)
            + _nc(112.7, 103.81),
            set(),
        ),
        # ...but it does with a junction there.
        (
            _wire(107.62, 98.73, 117.78, 98.73)
            + _wire(112.7, 98.73, 112.7, 103.81)
            + _nc(112.7, 103.81)
            + "(junction (at 112.7 98.73))",
            {"3"},
        ),
        # A flag on a wire's interior is not connected to it.
        (_wire(107.62, 98.73, 117.78, 98.73) + _nc(112.7, 98.73), set()),
        # Nor is a pin on a wire's interior: the wire runs through pin 2
        # (92.38, 100) from pin 1, and only pin 1 carries the flag.
        (_wire(92.38, 97.46, 92.38, 102.54) + _nc(92.38, 97.46), {"1"}),
        # A dangling flag on no pin and no wire flags nothing.
        (_nc(150, 150), set()),
    ],
    ids=["stub", "chain", "bare-tee", "junction-tee", "flag-mid-wire", "pin-mid-wire", "dangling"],
)
def test_flag_reaches_pin_through_wires(tmp_path: Path, extra: str, expected: set[str]) -> None:
    """KiCad flags every pin in the flag's wire-connected subgraph."""
    sch = _asym_sheet(tmp_path / "u.kicad_sch", extra=extra)
    assert _flagged(sch) == expected


# Unit-0 pin 5 sits at (100, 107.62) on unit 1 and at (100, 147.62) on unit 2.
@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ((100, 107.62), True),  # unit 1's copy only
        ((100, 147.62), False),  # unit 2's copy only
        (((100, 107.62), (100, 147.62)), True),  # both copies
    ],
    ids=["unit1-copy", "unit2-copy", "both-copies"],
)
def test_common_pin_takes_the_lowest_units_flag(tmp_path: Path, flags, expected: bool) -> None:
    """KiCad lists one netlist node per unit's copy; the pad takes the first.

    For unconnected copies that is the lowest unit (``unconnected-(U1A-...)``
    sorts first), so flags are not OR-ed across units.
    """
    points = flags if isinstance(flags[0], tuple) else (flags,)
    sch = _asym_sheet(
        tmp_path / "u.kicad_sch", units=(1, 2), extra=" ".join(_nc(x, y) for x, y in points)
    )
    assert schematic_pin_info(sch)[("U1", "5")].no_connect is expected


def test_flag_match_tolerates_half_grid_float_noise(tmp_path: Path) -> None:
    """A ``.xx5`` coordinate must match regardless of how it would round."""
    sch = _asym_sheet(tmp_path / "u.kicad_sch", extra="")
    text = sch.read_text().replace("(at 100 100 0)", "(at 100.635 100.005 0)")
    # Pin 3: (100.635 + 7.62, 100.005 - 1.27), written as KiCad would.
    sch.write_text(text.replace("\n)\n", "\n" + _nc(108.255, 98.735) + "\n)\n"))
    assert _flagged(sch) == {"3"}


def test_multiline_pad_replacement_leaves_no_blank_lines(design: tuple[Path, Path]) -> None:
    """Stale children on their own lines (KiCad layout) are replaced in place."""
    sch, pcb = design
    stale = _PCB.replace(
        '      (net 1 "MAIN")\n    )',
        '      (net 1 "MAIN")\n'
        '      (pinfunction "OLD")\n'
        '      (pintype "passive")\n'
        '      (uuid "00000000-0000-0000-0000-0000000000a1")\n'
        "    )",
    )
    assert stale != _PCB
    pcb.write_text(stale)
    annotate_pcb_file_pintypes(pcb, sch)
    text = pcb.read_text()
    assert (
        '    (pad "1" smd rect\n'
        "      (at -1 -1)\n"
        "      (size 0.6 0.6)\n"
        '      (layers "F.Cu")\n'
        '      (net 1 "MAIN")\n'
        '      (pinfunction "VIN")\n'
        '      (pintype "power_in")\n'
        '      (uuid "00000000-0000-0000-0000-0000000000a1")\n'
        "    )\n"
    ) in text
    assert "\n\n" not in text
    assert "OLD" not in text


def test_dry_run_does_not_write(design: tuple[Path, Path]) -> None:
    sch, pcb = design
    result = annotate_pcb_file_pintypes(pcb, sch, dry_run=True)
    assert result.updated == len(_EXPECTED)
    assert pcb.read_text() == _PCB


def test_object_annotation_round_trips_through_save(design: tuple[Path, Path]) -> None:
    """The PCB-object path (used by ``kct pcb sync-netlist``)."""
    sch, pcb_path = design
    pcb = PCB.load(pcb_path)
    result = annotate_pcb_pintypes(pcb, sch)
    assert result.updated == len(_EXPECTED)
    assert power_pin_nets(pcb) == {"MAIN", "VREG_1V2"}  # in-memory pads updated

    out = pcb_path.with_name("saved.kicad_pcb")
    pcb.save(out)
    assert _pad_children(out) == _EXPECTED
    assert power_pin_nets(PCB.load(out)) == {"MAIN", "VREG_1V2"}

    again = annotate_pcb_pintypes(PCB.load(out), sch)
    assert again.updated == 0


def test_sync_netlist_writes_pintypes(design: tuple[Path, Path]) -> None:
    """``kct pcb sync-netlist`` now mirrors KiCad's pad pin data."""
    from kicad_tools.cli.pcb_sync_netlist import SyncResult, _annotate_pintypes

    sch, pcb_path = design
    pcb = PCB.load(pcb_path)
    result = SyncResult()
    _annotate_pintypes(pcb, sch, result)
    assert result.pintype_updated == len(_EXPECTED)
    assert result.has_changes
    assert power_pin_nets(pcb) == {"MAIN", "VREG_1V2"}


def test_sync_netlist_keeps_no_connect_on_unconnected_net(design: tuple[Path, Path]) -> None:
    """``sync-netlist`` gives NC pins ``unconnected-(...)`` nets before
    annotating; the flagged pin must still get ``+no_connect``, exactly as
    the file path writes it."""
    from kicad_tools.cli.pcb_sync_netlist import SyncResult, _annotate_pintypes

    sch, pcb_path = design
    _flag_r1_pin2(sch)
    _r1_pin2_on_unconnected_net(pcb_path)
    pcb = PCB.load(pcb_path)
    _annotate_pintypes(pcb, sch, result := SyncResult())
    r1 = pcb.get_footprint("R1")
    assert r1 is not None
    assert {p.number: p.pintype for p in r1.pads} == {"1": "passive", "2": "passive+no_connect"}

    out = pcb_path.with_name("synced.kicad_pcb")
    pcb.save(out)
    annotate_pcb_file_pintypes(pcb_path, sch)
    assert _pad_children(out) == _pad_children(pcb_path)
    assert result.pintype_updated == len(_EXPECTED)


def test_cli_annotate_pintypes(design: tuple[Path, Path], capsys) -> None:
    from kicad_tools.cli import main

    sch, pcb = design
    rc = main(["pcb", "annotate-pintypes", str(pcb), "--schematic", str(sch), "--format", "json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["updated"] == len(_EXPECTED)
    assert payload["power_pin_nets"] == ["MAIN", "VREG_1V2"]
    assert _pad_children(pcb) == _EXPECTED


# ---------------------------------------------------------------------------
# Fleet board (acceptance criterion): board 04's committed design
# ---------------------------------------------------------------------------

_BOARD04 = REPO_ROOT / "boards" / "04-stm32-devboard" / "output"


@pytest.mark.parametrize(
    "pcb_name", ["stm32_devboard.kicad_pcb", "stm32_devboard_routed.kicad_pcb"]
)
def test_fleet_board04_power_pin_nets(tmp_path: Path, pcb_name: str) -> None:
    """Annotating board 04 from its own schematic yields its real rails.

    Runs on a copy: the committed outputs are hash-pinned by
    ``readiness.json`` and are refreshed by regenerating the board
    (``generate_design.py`` now calls ``annotate_pcb_file_pintypes``).
    """
    sch = _BOARD04 / "stm32_devboard.kicad_sch"
    src = _BOARD04 / pcb_name
    if not (sch.exists() and src.exists()):
        pytest.skip("board 04 outputs not present")
    pcb = tmp_path / pcb_name
    shutil.copy(src, pcb)

    result = annotate_pcb_file_pintypes(pcb, sch)
    assert result.missing_pads == []
    assert result.updated > 0

    loaded = PCB.load(pcb)
    # MCP1825S (U1): VI power_in on +5V, VO power_out on +3.3V; the STM32's
    # VDD/VBAT power_in pins sit on +3.3V.  GND is excluded as ground.
    assert power_pin_nets(loaded) == {"+3.3V", "+5V"}
    u1 = loaded.get_footprint("U1")
    assert u1 is not None
    by_net = {p.net_name: p.pintype for p in u1.pads if p.pintype.startswith("power")}
    assert by_net["+5V"] == "power_in"
    assert by_net["+3.3V"] == "power_out"


def test_fleet_board04_sync_netlist_matches_file_path(tmp_path: Path) -> None:
    """Both entry points write the same pin types on board 04.

    ``sync-netlist`` assigns ``unconnected-(...)`` nets to the 31 flagged
    STM32 pins before annotating; they must still come out
    ``bidirectional+no_connect``, as ``annotate-pintypes`` writes them.
    """
    from kicad_tools.cli.pcb_sync_netlist import sync_netlist

    sch = _BOARD04 / "stm32_devboard.kicad_sch"
    src = _BOARD04 / "stm32_devboard.kicad_pcb"
    if not (sch.exists() and src.exists()):
        pytest.skip("board 04 outputs not present")
    file_pcb = tmp_path / "file.kicad_pcb"
    sync_pcb = tmp_path / "sync.kicad_pcb"
    shutil.copy(src, file_pcb)
    shutil.copy(src, sync_pcb)

    annotate_pcb_file_pintypes(file_pcb, sch)
    result = sync_netlist(sch, sync_pcb)
    assert result.pintype_updated > 0

    def pintypes(path: Path) -> dict[tuple[str, str], str]:
        return {
            (fp.reference, pad.number): pad.pintype
            for fp in PCB.load(path).footprints
            for pad in fp.pads
            if pad.pintype
        }

    by_file = pintypes(file_pcb)
    assert sum(t == "bidirectional+no_connect" for t in by_file.values()) == 31
    assert pintypes(sync_pcb) == by_file

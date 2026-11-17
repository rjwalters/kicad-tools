"""Pin-type annotation for a sheet file placed more than once (issue #6004).

``kicad_tools.operations.pintype`` used to walk the hierarchy by sheet
*file*, so a ``.kicad_sch`` placed twice was read once and only the
references ``Schematic.load`` reports (the ``(property "Reference")``
values, ``R1``/``R2``) got pin data.  The second placement's footprints
(``R11``/``R12``) were left without ``pinfunction``/``pintype``.

The walk is now per placement, resolving each symbol's reference from its
``(instances (project ... (path "/<root>/<sheet-uuid>" (reference ...))))``
entry -- the resolution board LVS already uses (issue #5815).

Fixture: ``tests/fixtures/sheet_qualified_lvs/repeat_*`` -- one child sheet
(``R1``/``R2``) placed as ``MCU_A`` and ``MCU_B`` (``R11``/``R12``), with a
board carrying all four footprints.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kicad_tools.operations.netlist import build_pin_to_pad_map
from kicad_tools.operations.pintype import (
    annotate_pcb_file_pintypes,
    annotate_pcb_pintypes,
    schematic_pin_info,
)
from kicad_tools.schema.pcb import PCB

_FIXTURES = Path(__file__).parent / "fixtures" / "sheet_qualified_lvs"
_REFS = ("R1", "R2", "R11", "R12")
_ALL_PADS = {(ref, pad) for ref in _REFS for pad in ("1", "2")}


@pytest.fixture
def repeat_design(tmp_path: Path) -> tuple[Path, Path]:
    for name in ("repeat_root.kicad_sch", "repeat_child.kicad_sch", "repeat_board.kicad_pcb"):
        shutil.copy(_FIXTURES / name, tmp_path / name)
    return tmp_path / "repeat_root.kicad_sch", tmp_path / "repeat_board.kicad_pcb"


def _annotated_pads(pcb_path: Path) -> dict[tuple[str, str], str]:
    return {
        (fp.reference, pad.number): pad.pintype
        for fp in PCB.load(pcb_path).footprints
        for pad in fp.pads
        if pad.pintype
    }


def test_both_placements_resolve_their_own_references(repeat_design) -> None:
    sch, _ = repeat_design
    info = schematic_pin_info(sch)
    assert set(info) == _ALL_PADS
    assert all(data.pintype == "passive" for data in info.values())


def test_file_annotation_reaches_second_placement(repeat_design) -> None:
    sch, pcb = repeat_design
    result = annotate_pcb_file_pintypes(pcb, sch)
    assert result.missing_pads == []
    assert result.updated == len(_ALL_PADS)
    assert _annotated_pads(pcb) == dict.fromkeys(_ALL_PADS, "passive")


def test_object_annotation_reaches_second_placement(repeat_design) -> None:
    sch, pcb_path = repeat_design
    pcb = PCB.load(pcb_path)
    result = annotate_pcb_pintypes(pcb, sch)
    assert result.missing_pads == []
    assert result.updated == len(_ALL_PADS)
    assert {(fp.reference, p.number) for fp in pcb.footprints for p in fp.pads if p.pintype} == (
        _ALL_PADS
    )


def test_pin_to_pad_map_covers_second_placement(repeat_design) -> None:
    """``build_pin_to_pad_map`` shares the per-placement resolution."""
    sch, pcb_path = repeat_design
    mapping = build_pin_to_pad_map(sch, PCB.load(pcb_path))
    assert set(mapping) == _ALL_PADS


def test_no_connect_flags_apply_to_every_placement(repeat_design) -> None:
    """Flags are a property of the sheet file, so each placement shares them."""
    sch, _ = repeat_design
    child = sch.parent / "repeat_child.kicad_sch"
    text = child.read_text().rstrip()
    assert text.endswith(")")
    # R2 pin 2 end point (120, 53.81); its wire network also reaches R1 pin 2.
    child.write_text(
        text[:-1]
        + '\t(no_connect (at 120 53.81) (uuid "58150099-0000-4000-8000-000000005815"))\n)\n'
    )
    info = schematic_pin_info(sch)
    flagged = {key for key, data in info.items() if data.no_connect}
    assert flagged == {("R1", "2"), ("R2", "2"), ("R11", "2"), ("R12", "2")}


def test_matches_kicad_cli_netlist(repeat_design, tmp_path: Path) -> None:
    """Every ``(ref, pin)`` node of KiCad's netlist gets the same ``pintype``."""
    import subprocess

    from kicad_tools.cli.runner import find_kicad_cli
    from kicad_tools.sexp import parse_file

    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not installed")
    sch, _ = repeat_design
    out = tmp_path / "repeat.net"
    subprocess.run(
        [str(cli), "sch", "export", "netlist", "-o", str(out), str(sch)],
        check=True,
        capture_output=True,
    )
    expected: dict[tuple[str, str], str] = {}
    for node in parse_file(out).find_all("node"):
        ref = node.find_child("ref")
        pin = node.find_child("pin")
        ptype = node.find_child("pintype")
        if ref is None or pin is None or ptype is None:
            continue
        ref_s = ref.get_string(0) or ""
        if ref_s.startswith("#"):
            continue
        expected[(ref_s, pin.get_string(0) or "")] = ptype.get_string(0) or ""
    assert expected, "kicad-cli netlist carried no component nodes"
    info = schematic_pin_info(sch)
    assert {key: data.pintype for key, data in info.items()} == expected

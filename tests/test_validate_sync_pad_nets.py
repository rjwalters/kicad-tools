"""Regression tests for the per-pad net check in ``kct validate --sync`` (issue #5937).

``NetlistValidator._check_pad_net_assignments`` used to be a placeholder
whose loop body was ``pass``, so a PCB whose pads sat on the wrong nets
still reported ``in_sync: true``.  The check now compares each pad's PCB
net against the schematic pin's net **by connectivity**: two documents
that spell a net differently (``VCC`` vs ``/VCC``) but wire the same pads
together are in sync; a swapped, shorted or opened pad is not.

The fixture is board 00 (``boards/00-simple-led``): J1.1/R1.1 on ``VCC``,
R1.2/D1.2 on ``LED_ANODE``, J1.2/D1.1 on ``GND``.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from kicad_tools.validate.netlist import NetlistValidator

BOARD00 = Path(__file__).resolve().parents[1] / "boards" / "00-simple-led" / "output"
SCH = BOARD00 / "simple_led.kicad_sch"
PCB = BOARD00 / "simple_led.kicad_pcb"

pytestmark = pytest.mark.skipif(
    not (SCH.exists() and PCB.exists()), reason="board 00 artifacts not present"
)


def _pad_line(text: str, ref: str, pad: str) -> tuple[int, int]:
    """Return the (start, end) span of ``(pad "<pad>" ...)`` inside footprint ``ref``."""
    marker_candidates = [f'(fp_text reference "{ref}"', f'(property "Reference" "{ref}"']
    ref_pos = -1
    for marker in marker_candidates:
        ref_pos = text.find(marker)
        if ref_pos != -1:
            break
    assert ref_pos != -1, f"reference {ref} not in fixture"
    fp_start = text.rfind("(footprint ", 0, ref_pos)
    pad_start = text.find(f'(pad "{pad}"', fp_start)
    assert pad_start != -1, f"pad {ref}.{pad} not in fixture"
    pad_end = text.find("\n", pad_start)
    return pad_start, pad_end


def _set_pad_net(text: str, ref: str, pad: str, net: str | None) -> str:
    """Rewrite ``ref.pad``'s ``(net N "NAME")`` to ``net`` (``None`` drops it)."""
    start, end = _pad_line(text, ref, pad)
    line = text[start:end]
    if net is None:
        new_line = re.sub(r' \(net \d+ "[^"]*"\)', "", line)
    else:
        nets = {name: num for num, name in re.findall(r'^\s*\(net (\d+) "([^"]*)"\)', text, re.M)}
        assert net in nets, f"net {net} not declared in fixture"
        new_line = re.sub(r'\(net \d+ "[^"]*"\)', f'(net {nets[net]} "{net}")', line)
    assert new_line != line
    return text[:start] + new_line + text[end:]


def _fixture(tmp_path: Path, edits: list[tuple[str, str, str | None]]) -> tuple[Path, Path]:
    sch = tmp_path / SCH.name
    pcb = tmp_path / PCB.name
    shutil.copy(SCH, sch)
    text = PCB.read_text()
    for ref, pad, net in edits:
        text = _set_pad_net(text, ref, pad, net)
    pcb.write_text(text)
    return sch, pcb


def _pad_errors(sch: Path, pcb: Path) -> dict[str, tuple[str, str]]:
    result = NetlistValidator(sch, pcb).validate()
    return {
        f"{i.reference}.{i.pin}": (i.net_schematic, i.net_pcb)
        for i in result.errors
        if i.category == "net_mismatch" and i.pin
    }


def test_committed_board_is_in_sync() -> None:
    result = NetlistValidator(SCH, PCB).validate()
    assert result.in_sync, [i.message for i in result.issues]
    assert result.error_count == 0


def test_swapped_pad_nets_are_reported(tmp_path: Path) -> None:
    """The issue's repro: swap J1's pads between VCC and GND."""
    sch, pcb = _fixture(tmp_path, [("J1", "1", "GND"), ("J1", "2", "VCC")])
    result = NetlistValidator(sch, pcb).validate()
    assert not result.in_sync
    assert _pad_errors(sch, pcb) == {
        "J1.1": ("VCC", "GND"),
        "J1.2": ("GND", "VCC"),
    }


def test_cli_exits_nonzero_on_swapped_pads(tmp_path: Path, capsys) -> None:
    from kicad_tools.cli.validate_sync_cmd import main

    sch, pcb = _fixture(tmp_path, [("J1", "1", "GND"), ("J1", "2", "VCC")])
    rc = main([str(sch), str(pcb), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc != 0
    assert payload["in_sync"] is False
    flagged = {(i["reference"], i["pin"]) for i in payload["issues"]}
    assert {("J1", "1"), ("J1", "2")} <= flagged


def test_short_into_another_net_is_reported(tmp_path: Path) -> None:
    """R1.1 belongs on VCC; moving it onto LED_ANODE shorts R1."""
    sch, pcb = _fixture(tmp_path, [("R1", "1", "LED_ANODE")])
    assert _pad_errors(sch, pcb) == {"R1.1": ("VCC", "LED_ANODE")}


def test_unconnected_pad_on_multi_pin_net_is_reported(tmp_path: Path) -> None:
    sch, pcb = _fixture(tmp_path, [("D1", "1", None)])
    errors = _pad_errors(sch, pcb)
    assert set(errors) == {"D1.1"}
    assert errors["D1.1"][0] == "GND"


def test_net_renaming_alone_is_not_drift(tmp_path: Path) -> None:
    """Same connectivity under different names (``X`` vs ``/X``) is in sync."""
    sch, pcb = _fixture(tmp_path, [])
    text = pcb.read_text()
    for name in ("VCC", "GND", "LED_ANODE"):
        text = text.replace(f'"{name}"', f'"/{name}"')
    pcb.write_text(text)
    result = NetlistValidator(sch, pcb).validate()
    assert not [i for i in result.errors if i.pin], [i.message for i in result.errors]


def test_schematic_object_with_path_also_checks_pads(tmp_path: Path) -> None:
    """``Project.check_sync`` passes loaded objects; the pad check must still run."""
    from kicad_tools.schema.pcb import PCB as PCBClass
    from kicad_tools.schema.schematic import Schematic

    sch, pcb = _fixture(tmp_path, [("J1", "1", "GND"), ("J1", "2", "VCC")])
    result = NetlistValidator(Schematic.load(sch), PCBClass.load(str(pcb))).validate()
    assert {(i.reference, i.pin) for i in result.errors} == {("J1", "1"), ("J1", "2")}

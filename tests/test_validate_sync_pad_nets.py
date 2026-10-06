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


# --- Issue #5980: swapped net names and split-net wording -------------------

BOARD04 = Path(__file__).resolve().parents[1] / "boards" / "04-stm32-devboard" / "output"
SCH04 = BOARD04 / "stm32_devboard.kicad_sch"
PCB04 = BOARD04 / "stm32_devboard.kicad_pcb"
needs_board04 = pytest.mark.skipif(
    not (SCH04.exists() and PCB04.exists()), reason="board 04 artifacts not present"
)


def _rename_nets(text: str, mapping: dict[str, str]) -> str:
    """Rename PCB nets simultaneously (declarations and pad bindings alike).

    Renaming every ``(net N "A")`` to ``"B"`` and vice versa moves *all*
    pads of the two nets at once: the copper still joins the same pads,
    only the names are exchanged.
    """

    def sub(match: re.Match[str]) -> str:
        name = match.group(2)
        return f'(net {match.group(1)} "{mapping.get(name, name)}")'

    return re.sub(r'\(net (\d+) "([^"]*)"\)', sub, text)


def _name_errors(result) -> list:
    """Net-level (not per-pad) mismatch errors."""
    return [i for i in result.errors if i.category == "net_mismatch" and not i.pin]


def test_wholesale_name_swap_is_reported(tmp_path: Path) -> None:
    """Every VCC pad on PCB net ``GND`` and vice versa: no pad is out of place,
    but the names are exchanged, which is what net classes and zones key on."""
    sch, pcb = _fixture(tmp_path, [])
    pcb.write_text(_rename_nets(pcb.read_text(), {"VCC": "GND", "GND": "VCC"}))
    result = NetlistValidator(sch, pcb).validate()

    assert not result.in_sync
    assert _pad_errors(sch, pcb) == {}, "connectivity is intact; no pad should be blamed"
    swaps = _name_errors(result)
    assert len(swaps) == 1, [i.message for i in swaps]
    issue = swaps[0]
    assert issue.message.startswith("Net names swapped")
    assert {issue.net_schematic, issue.net_pcb} == {"GND", "VCC"}
    assert "'VCC'" in issue.message and "'GND'" in issue.message
    assert "Exchange the names of PCB nets" in issue.suggestion


def test_rotated_names_report_each_net(tmp_path: Path) -> None:
    """A three-way rotation has no mutual pair: each net is reported once."""
    sch, pcb = _fixture(tmp_path, [])
    rotate = {"VCC": "GND", "GND": "LED_ANODE", "LED_ANODE": "VCC"}
    pcb.write_text(_rename_nets(pcb.read_text(), rotate))
    result = NetlistValidator(sch, pcb).validate()

    assert not result.in_sync
    assert _pad_errors(sch, pcb) == {}
    # ``LED_ANODE`` is a local label, so its schematic identity is ``/LED_ANODE``.
    named = {(i.net_schematic.lstrip("/"), i.net_pcb) for i in _name_errors(result)}
    assert named == set(rotate.items())
    assert all("name of schematic net" in i.message for i in _name_errors(result))


def test_rename_to_unclaimed_name_is_not_a_swap(tmp_path: Path) -> None:
    """A PCB name that spells no schematic net is plain renaming, not drift."""
    sch, pcb = _fixture(tmp_path, [])
    pcb.write_text(_rename_nets(pcb.read_text(), {"LED_ANODE": "ANODE_NODE"}))
    result = NetlistValidator(sch, pcb).validate()
    assert not [i for i in result.errors if i.category == "net_mismatch"], [
        i.message for i in result.errors
    ]


def test_slash_spelling_is_not_a_swap(tmp_path: Path) -> None:
    """``/VCC`` on the PCB still spells schematic ``VCC``; nothing to report."""
    sch, pcb = _fixture(tmp_path, [])
    pcb.write_text(_rename_nets(pcb.read_text(), {"VCC": "/VCC", "GND": "/GND"}))
    assert NetlistValidator(sch, pcb).validate().in_sync


@needs_board04
def test_board04_full_swd_swap_is_reported(tmp_path: Path) -> None:
    """The issue's repro: all SWDIO pads on SWCLK and vice versa."""
    sch = tmp_path / SCH04.name
    pcb = tmp_path / PCB04.name
    shutil.copy(SCH04, sch)
    pcb.write_text(_rename_nets(PCB04.read_text(), {"SWDIO": "SWCLK", "SWCLK": "SWDIO"}))
    result = NetlistValidator(sch, pcb).validate()
    assert not result.in_sync
    swaps = _name_errors(result)
    assert len(swaps) == 1 and swaps[0].message.startswith("Net names swapped")
    assert "SWDIO" in swaps[0].message and "SWCLK" in swaps[0].message


def _board04_fixture(tmp_path: Path, text: str) -> tuple[Path, Path]:
    sch = tmp_path / SCH04.name
    pcb = tmp_path / PCB04.name
    shutil.copy(SCH04, sch)
    pcb.write_text(text)
    return sch, pcb


@needs_board04
def test_split_net_names_the_moved_pads_not_the_correct_one(tmp_path: Path) -> None:
    """Two of OSC_IN's three pads split off onto a new net.

    The one pad still on ``OSC_IN`` is the one that gets reported (the net
    pairs with the majority), so the message must say the net is split and
    name the pads that moved, not present ``U2.5`` as wrong with
    "expected PCB net 'Net-X'".
    """
    text = PCB04.read_text()
    text = text.replace('  (net 12 "BOOT0")\n', '  (net 12 "BOOT0")\n  (net 13 "Net-X")\n', 1)
    assert '(net 13 "Net-X")' in text
    for ref, pad in (("C10", "1"), ("Y1", "1")):
        text = _set_pad_net(text, ref, pad, "Net-X")
    sch, pcb = _board04_fixture(tmp_path, text)
    result = NetlistValidator(sch, pcb).validate()

    assert not result.in_sync
    pads = [i for i in result.errors if i.pin]
    assert [(i.reference, i.pin) for i in pads] == [("U2", "5")]
    msg = pads[0].message
    assert "is split on the PCB" in msg
    assert "this pad is on 'OSC_IN'" in msg
    assert "2 of the net's 3 pads (C10.1, Y1.1) are on 'Net-X'" in msg
    assert "expected PCB net" not in msg
    assert "C10.1, Y1.1" in pads[0].suggestion
    # A tool-generated name is never "the name of another net".
    assert not _name_errors(result)


@needs_board04
def test_rail_swap_with_stray_pad_reports_swap_and_split(tmp_path: Path) -> None:
    """GND/+3.3V names exchanged except one GND pad left on PCB ``GND``.

    Before #5980 this read ``C1.2: schematic net 'GND', PCB net 'GND'
    (expected PCB net '+3.3V')`` -- blaming the one pad whose name is right.
    """
    text = _rename_nets(PCB04.read_text(), {"GND": "+3.3V", "+3.3V": "GND"})
    text = _set_pad_net(text, "C1", "2", "GND")
    sch, pcb = _board04_fixture(tmp_path, text)
    result = NetlistValidator(sch, pcb).validate()

    assert not result.in_sync
    swaps = _name_errors(result)
    assert len(swaps) == 1 and swaps[0].message.startswith("Net names swapped")
    assert {swaps[0].net_schematic, swaps[0].net_pcb} == {"+3.3V", "GND"}
    pads = {(i.reference, i.pin): i.message for i in result.errors if i.pin}
    assert set(pads) == {("C1", "2")}
    assert "schematic net 'GND' is split on the PCB" in pads[("C1", "2")]
    assert "are on '+3.3V'" in pads[("C1", "2")]

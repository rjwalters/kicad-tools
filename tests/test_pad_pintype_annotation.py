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


def test_unconnected_pad_gets_no_connect_suffix(design: tuple[Path, Path]) -> None:
    """KiCad marks an unconnected pin's pad ``<type>+no_connect``."""
    sch, pcb = design
    pcb.write_text(_PCB.replace(' (net 4 "EN"))', ")"))
    annotate_pcb_file_pintypes(pcb, sch)
    assert _pad_children(pcb)[("R1", "2")] == ("", "passive+no_connect")


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

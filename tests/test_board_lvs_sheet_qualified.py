"""Sheet-qualified local net identities in board LVS (issues #5815 / #5809).

A **local label** names a net only inside the sheet it is drawn on. KiCad's
netlister therefore writes it prefixed by that sheet's path — ``/SENSE`` for a
root-sheet label, ``/MCU/DBG_LED`` for one inside a child sheet named ``MCU``
— and the board inherits those names. ``_schematic_pin_to_net`` used to emit
the bare label text for every sheet, which produced two distinct defects:

* every hierarchical local net disagreed with the board, so ``kct check``
  reported ``label: N mismatch(es)`` on a correct design (#5815 for child
  sheets, #5809 for the root sheet);
* two sibling sheets reusing the same label text (``DBG_LED`` in both ``MCU``
  and ``AUX``) collapsed into **one** schematic identity, which the copper leg
  then had to read as a short between unrelated nets.

The fixture under ``tests/fixtures/sheet_qualified_lvs/`` is a reduced version
of the design in #5815 and is deliberately library-free so ``kicad-cli`` can
read it without KiCad's symbol libraries installed:

* ``root_sq.kicad_sch`` — R10/R11/R12 plus two sheet symbols (``MCU``, ``AUX``).
  Root-local label ``SENSE`` joins R11.1/R12.1 (#5809's shape); local label
  ``BUS`` names the wire feeding the ``MCU`` sheet pin of the same name;
  ``GLOBAL_NET`` is a global label and ``GND`` a power net.
* ``sub_mcu_sq.kicad_sch`` — R1/R2; local label ``DBG_LED`` joins R1.2/R2.2,
  hierarchical label ``BUS`` mates with the parent's sheet pin, and R2.1
  carries the shared ``GLOBAL_NET`` global label.
* ``sub_aux_sq.kicad_sch`` — R3/R4; local label ``DBG_LED`` **again**, joining
  R3.2/R4.2, electrically unrelated to the MCU one.
* ``board_sq.kicad_pcb`` — the matching board, nets spelled the way KiCad
  spells them (``/MCU/DBG_LED``, ``/AUX/DBG_LED``, ``/SENSE``, ``/BUS``,
  ``GLOBAL_NET``, ``GND``).

Verified against KiCad 10.0.6: ``kicad-cli sch export netlist --format
kicadxml`` on this fixture yields exactly those six nets with exactly those
pad sets (see ``test_identities_match_native_kicad_netlist``).
"""

from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from kicad_tools.lvs.board_lvs import (
    _is_sheet_path_variant,
    _pcb_pin_to_net,
    _schematic_pin_to_net,
    _walk_hierarchy_schematics,
    compare_netlists,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "sheet_qualified_lvs"
_ROOT_SCH = _FIXTURES / "root_sq.kicad_sch"
_BOARD_PCB = _FIXTURES / "board_sq.kicad_pcb"

# The identity every pad resolves to after the fix.  Only *local* labels are
# sheet-qualified: ``GLOBAL_NET`` (global label), ``GND`` (power symbol) and
# ``BUS`` (a local label whose text is also a sheet pin / hierarchical label,
# i.e. a net that legitimately spans two sheets) stay unqualified.
_EXPECTED_SCH_IDENTITIES = {
    ("R1", "1"): "BUS",
    ("R1", "2"): "/MCU/DBG_LED",
    ("R2", "1"): "GLOBAL_NET",
    ("R2", "2"): "/MCU/DBG_LED",
    ("R3", "1"): "GND",
    ("R3", "2"): "/AUX/DBG_LED",
    ("R4", "1"): "GND",
    ("R4", "2"): "/AUX/DBG_LED",
    ("R10", "1"): "BUS",
    ("R10", "2"): "GND",
    ("R11", "1"): "/SENSE",
    ("R11", "2"): "GLOBAL_NET",
    ("R12", "1"): "/SENSE",
    ("R12", "2"): "GND",
}


def _rewrite_board(tmp_path: Path, replacements: dict[str, str], name: str) -> Path:
    """Copy the board fixture with literal net-name substitutions applied."""
    text = _BOARD_PCB.read_text()
    for old, new in replacements.items():
        text = text.replace(old, new)
    out = tmp_path / name
    out.write_text(text)
    return out


# --------------------------------------------------------------------------
# Sheet-path tracking
# --------------------------------------------------------------------------


def test_walker_reports_each_sheets_path() -> None:
    """The hierarchy walk carries the KiCad sheet path of every sheet.

    The path component is the sheet symbol's ``Sheetname`` property (``MCU``),
    not its ``Sheetfile`` (``sub_mcu_sq.kicad_sch``) — that is what KiCad puts
    in a net name.
    """
    visits = list(_walk_hierarchy_schematics(_ROOT_SCH))

    assert [v.sheet_path for v in visits] == ["/", "/MCU/", "/AUX/"]
    # The root declares one sheet pin (``BUS``); the children declare none.
    assert [sorted(v.sheet_pin_names) for v in visits] == [["BUS"], [], []]


# --------------------------------------------------------------------------
# Schematic-side identities
# --------------------------------------------------------------------------


def test_local_labels_are_sheet_qualified() -> None:
    """Local labels gain their sheet path; shared names do not (#5815/#5809)."""
    assert _schematic_pin_to_net(_ROOT_SCH) == _EXPECTED_SCH_IDENTITIES


def test_sibling_sheets_keep_same_local_label_distinct() -> None:
    """``DBG_LED`` in ``MCU`` and in ``AUX`` are two nets, not one.

    Before the fix both resolved to the bare string ``DBG_LED``, merging four
    pads from two unrelated nets into a single schematic identity — the input
    from which ``copper_lvs.compare_partitions`` manufactures false shorts.
    """
    pin_map = _schematic_pin_to_net(_ROOT_SCH)

    mcu_pads = {k for k, v in pin_map.items() if v == "/MCU/DBG_LED"}
    aux_pads = {k for k, v in pin_map.items() if v == "/AUX/DBG_LED"}

    assert mcu_pads == {("R1", "2"), ("R2", "2")}
    assert aux_pads == {("R3", "2"), ("R4", "2")}
    assert not mcu_pads & aux_pads
    # No pad is left on the ambiguous bare name.
    assert "DBG_LED" not in set(pin_map.values())


def test_global_labels_power_nets_and_sheet_pins_stay_unqualified() -> None:
    """Regression guard against over-qualifying (#5815 acceptance criterion).

    * ``GLOBAL_NET`` is a global label and is shared between the root sheet
      and the ``MCU`` child — one identity, no path.
    * ``GND`` is a power net spanning the root and ``AUX`` — one identity.
    * ``BUS`` is a *local* label on the root, but its text is also the ``MCU``
      sheet pin / hierarchical label that carries the net into the child
      sheet.  Qualifying it would split the parent half (``/BUS``) from the
      child half (``BUS``) and break the sheet-pin unification this walker
      already had.
    """
    pin_map = _schematic_pin_to_net(_ROOT_SCH)

    assert {k for k, v in pin_map.items() if v == "GLOBAL_NET"} == {
        ("R11", "2"),
        ("R2", "1"),
    }
    assert {k for k, v in pin_map.items() if v == "GND"} == {
        ("R10", "2"),
        ("R12", "2"),
        ("R3", "1"),
        ("R4", "1"),
    }
    assert {k for k, v in pin_map.items() if v == "BUS"} == {("R1", "1"), ("R10", "1")}


# --------------------------------------------------------------------------
# Agreement with KiCad's own netlister
# --------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not installed")
def test_identities_match_native_kicad_netlist(tmp_path: Path) -> None:
    """kct's identities agree with ``kicad-cli sch export netlist`` (#5815).

    Two claims are made, and they are different strengths:

    * **Partition equality** — the pads kct groups per net are exactly the
      pads KiCad groups per net.  This is the semantic claim; it holds for
      every net including ``BUS``, which kct spells bare and KiCad spells
      ``/BUS`` (a sheet-pin net is named at the highest sheet that drives it,
      which kct does not yet resolve — issue #4099 Phase 2 — and which
      ``compare_netlists`` forgives only after checking the pads).
    * **Exact name equality for the local-label nets** — the identities this
      issue is about (``/SENSE``, ``/MCU/DBG_LED``, ``/AUX/DBG_LED``) are
      byte-identical to KiCad's.
    """
    work = tmp_path / "sch"
    work.mkdir()
    for sch in _FIXTURES.glob("*.kicad_sch"):
        shutil.copy(sch, work / sch.name)
    out_xml = work / "native.xml"
    subprocess.run(
        [
            "kicad-cli",
            "sch",
            "export",
            "netlist",
            "--format",
            "kicadxml",
            "-o",
            str(out_xml),
            str(work / _ROOT_SCH.name),
        ],
        check=True,
        capture_output=True,
        timeout=300,
    )

    native: dict[str, set[tuple[str, str]]] = {}
    for net in ET.parse(out_xml).getroot().find("nets"):  # type: ignore[union-attr]
        name = net.get("name") or ""
        native[name] = {(n.get("ref") or "", n.get("pin") or "") for n in net.findall("node")}

    kct_map = _schematic_pin_to_net(_ROOT_SCH)
    kct: dict[str, set[tuple[str, str]]] = {}
    for key, value in kct_map.items():
        if value is not None:
            kct.setdefault(value, set()).add(key)

    # Same connectivity: identical pad partitions on both sides.
    assert sorted(kct.values(), key=sorted) == sorted(native.values(), key=sorted)

    # Same spelling for every net named by a local label.
    for name in ("/SENSE", "/MCU/DBG_LED", "/AUX/DBG_LED"):
        assert name in native, f"fixture drifted: {name} absent from native netlist"
        assert kct.get(name) == native[name]


# --------------------------------------------------------------------------
# End-to-end LVS verdicts
# --------------------------------------------------------------------------


def test_label_lvs_clean_against_kicad_spelled_board() -> None:
    """The correct board passes: no false ``label:`` mismatches (#5815)."""
    assert _pcb_pin_to_net(_BOARD_PCB)[("R1", "2")] == "/MCU/DBG_LED"

    result = compare_netlists(_ROOT_SCH, _BOARD_PCB)

    assert result.vacuous is False
    assert result.mismatches == ()
    assert result.clean is True


def test_label_lvs_clean_against_board_without_leading_slash(tmp_path: Path) -> None:
    """The exact spelling gap reported in #5815 is tolerated.

    The board observed there carried ``MCU/DBG_LED`` while KiCad's own netlist
    said ``/MCU/DBG_LED``; the reporter's independent check passed after
    "normalizing only a leading slash".  Here the tolerance is earned per-net
    by pad-set equality, not applied blindly.
    """
    board = _rewrite_board(
        tmp_path,
        {'"/MCU/': '"MCU/', '"/AUX/': '"AUX/', '"/SENSE"': '"SENSE"', '"/BUS"': '"BUS"'},
        "unslashed.kicad_pcb",
    )
    assert _pcb_pin_to_net(board)[("R1", "2")] == "MCU/DBG_LED"
    assert _pcb_pin_to_net(board)[("R11", "1")] == "SENSE"

    result = compare_netlists(_ROOT_SCH, board)

    assert result.mismatches == ()
    assert result.clean is True


def test_wrong_pcb_net_assignment_still_fails(tmp_path: Path) -> None:
    """Negative control: one pad moved to the sibling sheet's net must fail.

    R3.2 belongs to ``/AUX/DBG_LED``.  Binding it to ``/MCU/DBG_LED`` on the
    board is a real defect, and the sheet-path tolerance must not swallow it:
    the pad sets on the two sides no longer agree.
    """
    text = _BOARD_PCB.read_text()
    # Rewrite only R3's pad 2 (the footprint block is emitted ref-by-ref).
    head, _, tail = text.partition('(uuid "sqlvs-fp-r3")')
    tail_fixed = tail.replace('(net 1 "/AUX/DBG_LED")', '(net 3 "/MCU/DBG_LED")', 1)
    assert tail_fixed != tail
    board = tmp_path / "wrong.kicad_pcb"
    board.write_text(head + '(uuid "sqlvs-fp-r3")' + tail_fixed)

    result = compare_netlists(_ROOT_SCH, board)

    assert result.clean is False
    assert result.vacuous is False
    offenders = {(m.ref, m.pad): (m.schematic_net, m.pcb_net) for m in result.mismatches}
    # Exactly the misbound pad is reported: the other three pads' names agree
    # byte-for-byte on both sides, so they never reach the tolerance at all.
    assert offenders == {("R3", "2"): ("/AUX/DBG_LED", "/MCU/DBG_LED")}


def test_swapping_two_sibling_sheet_nets_still_fails(tmp_path: Path) -> None:
    """Naming ``MCU``'s net ``/AUX/DBG_LED`` (and vice versa) must fail.

    Both nets keep their pad sets, so a pads-only check would pass.  Two
    different sheet paths are not "the same name with a different prefix",
    so the tolerance never applies and the mix-up is reported.
    """
    board = _rewrite_board(
        tmp_path,
        {'"/MCU/DBG_LED"': '"@TMP@"', '"/AUX/DBG_LED"': '"/MCU/DBG_LED"'},
        "swapped.kicad_pcb",
    )
    board.write_text(board.read_text().replace('"@TMP@"', '"/AUX/DBG_LED"'))

    result = compare_netlists(_ROOT_SCH, board)

    assert result.clean is False
    assert {(m.ref, m.pad) for m in result.mismatches} == {
        ("R1", "2"),
        ("R2", "2"),
        ("R3", "2"),
        ("R4", "2"),
    }


def test_board_that_merges_both_sibling_nets_still_fails(tmp_path: Path) -> None:
    """An *unqualified* board name facing two sibling nets is a real short.

    Spelling both sheets' nets ``DBG_LED`` on the board joins four pads that
    the schematic keeps in two nets.  The suffix rule nominates the pair, but
    the pad sets differ (2 vs 4), so the verdict stays dirty.
    """
    board = _rewrite_board(
        tmp_path,
        {'"/MCU/DBG_LED"': '"DBG_LED"', '"/AUX/DBG_LED"': '"DBG_LED"'},
        "merged.kicad_pcb",
    )

    result = compare_netlists(_ROOT_SCH, board)

    assert result.clean is False
    assert {(m.ref, m.pad) for m in result.mismatches} == {
        ("R1", "2"),
        ("R2", "2"),
        ("R3", "2"),
        ("R4", "2"),
    }


def test_unrelated_net_name_change_is_not_a_path_variant(tmp_path: Path) -> None:
    """The tolerance is about paths only: a different leaf still mismatches."""
    board = _rewrite_board(tmp_path, {'"/SENSE"': '"/FEEDBACK"'}, "renamed.kicad_pcb")

    result = compare_netlists(_ROOT_SCH, board)

    assert result.clean is False
    assert {(m.ref, m.pad) for m in result.mismatches} == {("R11", "1"), ("R12", "1")}


# --------------------------------------------------------------------------
# The nomination predicate itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sch_net", "pcb_net", "expected"),
    [
        ("/MCU/DBG_LED", "MCU/DBG_LED", True),  # leading-slash convention
        ("/SENSE", "SENSE", True),  # root sheet, unqualified board
        ("/MCU/DBG_LED", "DBG_LED", True),  # fully unqualified board
        ("/MCU/DBG_LED", "/AUX/DBG_LED", False),  # different sheets
        ("/MCU/DBG_LED", "/MCU/DBG_LED2", False),  # different leaf
        ("/SENSE", "/FEEDBACK", False),
        ("/MCU/DBG_LED", None, False),  # unconnected board pad
        (None, "/MCU/DBG_LED", False),  # pad absent from the schematic
        ("Net-(R1-2)", "Net-(/MCU/R1-Pad2)", False),  # placeholders: other rule
        ("GND", "GND", False),  # identical names are not "variants"
    ],
)
def test_sheet_path_variant_predicate(
    sch_net: str | None, pcb_net: str | None, expected: bool
) -> None:
    assert _is_sheet_path_variant(sch_net, pcb_net) is expected

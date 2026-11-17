"""Repeated sheet instances in board LVS (issue #5815).

Sheet-qualification of local labels landed for *distinct* sub-sheet files in
#5809 / #5826 (``tests/test_board_lvs_sheet_qualified*.py``).  This module
covers the remaining shape from #5815's acceptance criteria: **one sheet file
placed more than once**, the normal way a hierarchical design reuses a block.

KiCad treats every ``(sheet ...)`` *symbol* as its own sheet.  Placing
``repeat_child.kicad_sch`` as both ``MCU_A`` and ``MCU_B`` therefore yields
two independent sheets whose identically-spelled local label ``DBG_LED``
names two electrically distinct nets, ``/MCU_A/DBG_LED`` and
``/MCU_B/DBG_LED``, with two separately annotated sets of parts.  Verified
against ``kicad-cli`` 10.0.6 on this very fixture::

    /MCU_A/DBG_LED -> [(R1, 2), (R2, 2)]
    /MCU_B/DBG_LED -> [(R11, 2), (R12, 2)]
    GND            -> [(R1, 1), (R2, 1), (R11, 1), (R12, 1)]

Two defects had to be fixed to reproduce that:

* ``_walk_hierarchy_schematics`` de-duplicated by sheet **file**, so the
  second placement was never walked and *every* one of its pins was missing
  from the schematic-side map.  The cycle guard is now the ancestor chain of
  the branch being walked — which is what "circular" actually means — so
  sibling re-placement is free.
* a symbol's ``(property "Reference" ...)`` holds one value per file, not one
  per placement.  Both placements answered ``R1``/``R2`` and the second
  silently overwrote the first.  ``_instance_reference_map`` now applies the
  per-placement designator KiCad records in the symbol's ``(instances ...)``
  block, keyed by the placement's instance (UUID) path.

Fixtures (deliberately library-free, so ``kicad-cli`` reads them without
KiCad's symbol libraries installed):

* ``repeat_root.kicad_sch`` — no components, two sheet symbols ``MCU_A`` and
  ``MCU_B`` that share one ``Sheetfile``.
* ``repeat_child.kicad_sch`` — R1/R2 joined at pin 2 by the local label
  ``DBG_LED``, pin 1 of each on the ``GND`` power net; ``(instances ...)``
  annotates the ``MCU_B`` placement as R11/R12.
* ``repeat_board.kicad_pcb`` — the matching board, nets spelled KiCad's way.

The UUIDs in those fixtures are real RFC-4122 UUIDs on purpose: KiCad
regenerates any ``(uuid ...)`` whose value does not parse as one, which
breaks instance-path lookup and silently costs the per-placement
annotation.
"""

from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from kicad_tools.lvs import board_lvs
from kicad_tools.lvs.board_lvs import (
    _pcb_pin_to_net,
    _schematic_pin_to_net,
    _walk_hierarchy_schematics,
    compare_netlists,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "sheet_qualified_lvs"
_ROOT_SCH = _FIXTURES / "repeat_root.kicad_sch"
_CHILD_SCH = _FIXTURES / "repeat_child.kicad_sch"
_BOARD_PCB = _FIXTURES / "repeat_board.kicad_pcb"

# Every pad, with the identity KiCad gives it.  The two placements of one
# file contribute four distinct parts and two distinct local nets; the
# power net spans both placements and is not qualified.
_EXPECTED_SCH_IDENTITIES = {
    ("R1", "1"): "GND",
    ("R1", "2"): "/MCU_A/DBG_LED",
    ("R2", "1"): "GND",
    ("R2", "2"): "/MCU_A/DBG_LED",
    ("R11", "1"): "GND",
    ("R11", "2"): "/MCU_B/DBG_LED",
    ("R12", "1"): "GND",
    ("R12", "2"): "/MCU_B/DBG_LED",
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
# Walking every placement
# --------------------------------------------------------------------------


def test_walker_visits_every_placement_of_a_shared_sheet_file() -> None:
    """Both placements of one ``Sheetfile`` are walked, not just the first.

    Before #5815 a hierarchy-wide ``visited`` set of resolved paths stopped
    the walk at the first placement, so ``/MCU_B`` never appeared and none of
    its pins reached the schematic-side map.
    """
    visits = list(_walk_hierarchy_schematics(_ROOT_SCH))

    assert [v.sheet_path for v in visits] == ["", "/MCU_A", "/MCU_B"]
    # Same file behind both child visits -- that is the whole point.
    assert visits[1].source.resolve() == visits[2].source.resolve() == _CHILD_SCH.resolve()
    # ...but distinct instance paths, which is what tells them apart.
    assert visits[1].uuid_path != visits[2].uuid_path
    assert visits[1].uuid_path.startswith(visits[0].uuid_path + "/")
    assert visits[2].uuid_path.startswith(visits[0].uuid_path + "/")


def _with_loop_sheet(work: Path, target: str) -> Path:
    """Copy the fixture set into ``work``, adding a child sheet -> ``target``."""
    for sch in _FIXTURES.glob("repeat_*.kicad_sch"):
        shutil.copy(sch, work / sch.name)
    child = work / _CHILD_SCH.name
    loop_sheet = (
        "\t(sheet\n\t\t(at 60 80) (size 20 10)\n"
        '\t\t(uuid "58159999-0000-4000-8000-000000005815")\n'
        '\t\t(property "Sheetname" "LOOP" (at 60 79 0))\n'
        f'\t\t(property "Sheetfile" "{target}" (at 60 91 0))\n'
        "\t)\n"
    )
    text = child.read_text()
    child.write_text(text[: text.rindex(")")] + loop_sheet + ")\n")
    return work / _ROOT_SCH.name


@pytest.mark.parametrize("target", ["repeat_child.kicad_sch", "repeat_root.kicad_sch"])
def test_circular_reference_is_still_refused(tmp_path: Path, target: str) -> None:
    """Relaxing the guard to ancestors must not reopen infinite recursion.

    Two shapes of cycle, both of which the old hierarchy-wide ``visited`` set
    covered incidentally and the ancestor guard must still catch:

    * ``repeat_child.kicad_sch`` — the sheet instantiates **itself**;
    * ``repeat_root.kicad_sch`` — it instantiates an ancestor further up.

    In both cases the offending edge is refused *before* the sheet is
    yielded, so the walk is exactly the acyclic hierarchy.  The sibling
    re-placement of ``repeat_child`` under ``MCU_A``/``MCU_B`` is untouched:
    that file is not an ancestor of either placement.
    """
    root = _with_loop_sheet(tmp_path, target)

    visits = list(_walk_hierarchy_schematics(root))

    assert [v.sheet_path for v in visits] == ["", "/MCU_A", "/MCU_B"]


def test_pathological_expansion_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """The placement cap raises rather than letting the walk run away.

    Per-placement expansion is exponential in the worst case, so the walk is
    bounded.  A design that trips the bound must say so, not silently return
    a truncated hierarchy -- silently dropping sheets is the very failure
    #5815 is about.
    """
    monkeypatch.setattr(board_lvs, "_MAX_SHEET_VISITS", 2)

    with pytest.raises(ValueError, match="sheet placements"):
        list(_walk_hierarchy_schematics(_ROOT_SCH))


# --------------------------------------------------------------------------
# Schematic-side identities
# --------------------------------------------------------------------------


def test_repeated_placements_get_per_placement_references() -> None:
    """``MCU_B``'s pads are R11/R12, not a second copy of R1/R2.

    ``(property "Reference" ...)`` stores one designator per *file*; the
    per-placement designators live in each symbol's ``(instances ...)``
    block.  Without applying them the second placement's pins overwrite the
    first's under identical keys, and half the board silently loses its
    schematic side.
    """
    pin_map = _schematic_pin_to_net(_ROOT_SCH)

    assert {ref for ref, _ in pin_map} == {"R1", "R2", "R11", "R12"}
    assert pin_map == _EXPECTED_SCH_IDENTITIES


def test_repeated_placements_keep_the_same_local_label_distinct() -> None:
    """One ``DBG_LED`` label, two placements, two nets (#5815).

    This is the criterion a bare-label model gets wrong in the most dangerous
    direction: merging two unrelated nets into one identity is the input from
    which ``copper_lvs.compare_partitions`` manufactures false shorts.
    """
    pin_map = _schematic_pin_to_net(_ROOT_SCH)

    a_pads = {k for k, v in pin_map.items() if v == "/MCU_A/DBG_LED"}
    b_pads = {k for k, v in pin_map.items() if v == "/MCU_B/DBG_LED"}

    assert a_pads == {("R1", "2"), ("R2", "2")}
    assert b_pads == {("R11", "2"), ("R12", "2")}
    assert not a_pads & b_pads
    # Nothing is left on the ambiguous bare name.
    assert "DBG_LED" not in set(pin_map.values())


def test_power_net_stays_shared_across_placements() -> None:
    """Regression guard against over-qualifying.

    ``GND`` is a power net, not a local label.  It must keep one identity
    spanning both placements -- ``/MCU_A/GND`` and ``/MCU_B/GND`` would split
    one rail into two and fail LVS on a correct board.
    """
    pin_map = _schematic_pin_to_net(_ROOT_SCH)

    assert {k for k, v in pin_map.items() if v == "GND"} == {
        ("R1", "1"),
        ("R2", "1"),
        ("R11", "1"),
        ("R12", "1"),
    }
    assert not any(v and v.endswith("/GND") for v in pin_map.values())


# --------------------------------------------------------------------------
# Agreement with KiCad's own netlister
# --------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not installed")
def test_identities_match_native_kicad_netlist(tmp_path: Path) -> None:
    """kct agrees with ``kicad-cli sch export netlist`` on the repeated case.

    Both the pad partitions and the two local nets' exact spellings, so the
    fixture cannot drift into asserting a convention KiCad does not use.
    """
    work = tmp_path / "sch"
    work.mkdir()
    for sch in _FIXTURES.glob("repeat_*.kicad_sch"):
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

    assert sorted(kct.values(), key=sorted) == sorted(native.values(), key=sorted)
    for name in ("/MCU_A/DBG_LED", "/MCU_B/DBG_LED", "GND"):
        assert name in native, f"fixture drifted: {name} absent from native netlist"
        assert kct.get(name) == native[name]


# --------------------------------------------------------------------------
# End-to-end LVS verdicts
# --------------------------------------------------------------------------


def test_label_lvs_clean_against_kicad_spelled_board() -> None:
    """The correct board passes: no false ``label:`` mismatches (#5815)."""
    assert _pcb_pin_to_net(_BOARD_PCB)[("R11", "2")] == "/MCU_B/DBG_LED"

    result = compare_netlists(_ROOT_SCH, _BOARD_PCB)

    assert result.vacuous is False
    assert result.mismatches == ()
    assert result.clean is True


def test_board_that_merges_the_two_placements_still_fails(tmp_path: Path) -> None:
    """Collapsing both placements onto one net is a short and must fail.

    Each placement's local ``DBG_LED`` is its own net.  A board that binds all
    four pads to a single ``DBG_LED`` has shorted them, and no leading-slash
    or sheet-path tolerance may excuse it: the bare name is a variant of *two*
    schematic nets, so it is a variant of neither.
    """
    board = _rewrite_board(
        tmp_path,
        {'"/MCU_A/DBG_LED"': '"DBG_LED"', '"/MCU_B/DBG_LED"': '"DBG_LED"'},
        "merged.kicad_pcb",
    )

    result = compare_netlists(_ROOT_SCH, board)

    assert result.clean is False
    assert result.vacuous is False
    offenders = {(m.ref, m.pad) for m in result.mismatches}
    assert offenders == {("R1", "2"), ("R2", "2"), ("R11", "2"), ("R12", "2")}


def test_swapping_the_two_placements_nets_still_fails(tmp_path: Path) -> None:
    """Naming ``MCU_A``'s net ``/MCU_B/DBG_LED`` (and vice versa) must fail.

    Both nets keep their pad *sizes*, so a count-only check would pass.  Two
    different sheet paths are never spelling variants of each other.
    """
    board = _rewrite_board(
        tmp_path,
        {'"/MCU_A/DBG_LED"': '"__TMP__"', '"/MCU_B/DBG_LED"': '"/MCU_A/DBG_LED"'},
        "swapped.kicad_pcb",
    )
    board.write_text(board.read_text().replace('"__TMP__"', '"/MCU_B/DBG_LED"'))

    result = compare_netlists(_ROOT_SCH, board)

    assert result.clean is False
    offenders = {(m.ref, m.pad) for m in result.mismatches}
    assert offenders == {("R1", "2"), ("R2", "2"), ("R11", "2"), ("R12", "2")}


def test_wrong_pcb_net_assignment_still_fails(tmp_path: Path) -> None:
    """Negative control: one pad moved to the other placement's net must fail.

    R11.2 belongs to ``/MCU_B/DBG_LED``.  Binding it to ``/MCU_A/DBG_LED`` is
    a real defect and is reported on exactly that pad.
    """
    text = _BOARD_PCB.read_text()
    head, _, tail = text.partition('(uuid "rptlvs-fp-r11")')
    tail_fixed = tail.replace('(net 2 "/MCU_B/DBG_LED")', '(net 1 "/MCU_A/DBG_LED")', 1)
    assert tail_fixed != tail
    board = tmp_path / "wrong.kicad_pcb"
    board.write_text(head + '(uuid "rptlvs-fp-r11")' + tail_fixed)

    result = compare_netlists(_ROOT_SCH, board)

    assert result.clean is False
    assert result.vacuous is False
    offenders = {(m.ref, m.pad): (m.schematic_net, m.pcb_net) for m in result.mismatches}
    assert offenders == {("R11", "2"): ("/MCU_B/DBG_LED", "/MCU_A/DBG_LED")}


def test_board_missing_the_second_placement_is_not_silently_clean(tmp_path: Path) -> None:
    """The original #5815 failure mode, inverted.

    When only the first placement was walked, the second placement's pads had
    no schematic side at all.  Whatever the board says about R11/R12 must be
    judged against a real schematic net, so dropping those footprints from
    the board is a mismatch rather than a pass.
    """
    text = _BOARD_PCB.read_text()
    start = text.index(
        '  (footprint "Resistor_SMD:R_0402_1005Metric"\n    (layer "F.Cu")\n    (uuid "rptlvs-fp-r11")'
    )
    board = tmp_path / "missing.kicad_pcb"
    board.write_text(text[:start] + ")\n")

    result = compare_netlists(_ROOT_SCH, board)

    assert result.clean is False
    offenders = {(m.ref, m.pad): m.pcb_net for m in result.mismatches}
    assert offenders == {
        ("R11", "1"): None,
        ("R11", "2"): None,
        ("R12", "1"): None,
        ("R12", "2"): None,
    }

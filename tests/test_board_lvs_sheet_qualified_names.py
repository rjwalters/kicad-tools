"""Sheet-qualified local net identities in label LVS (issues #5809, #5815).

KiCad scopes a plain ``(label "SENSE")`` to the sheet it sits on and names
the resulting net with that sheet's path: ``/SENSE`` on the root sheet,
``/ChildA/SENSE`` inside a sheet whose ``Sheetname`` is ``ChildA``.
``_schematic_pin_to_net`` used to return the bare label text, so every named
local net on a KiCad-created board was a false mismatch — ``SENSE`` versus
``/SENSE`` (issue #5809, 291 of them on the reporter's board) and
``DBG_LED`` versus ``/MCU/DBG_LED`` one level down (issue #5815).

What these tests pin down:

* the issue's own root-sheet reproduction is clean, and the schematic side
  reports the qualified identity;
* a **negative control** — the same fixture with the PCB's pad-to-net
  bindings swapped — still reports the swap, so the fix is a net-identity
  change and not a mismatch suppressor;
* three sheets carrying the *same* bare local label ``SENSE`` keep three
  distinct identities, and a board that collapses them onto one bare
  ``SENSE`` still fails;
* global labels, power-symbol nets and ``PWR_FLAG``-driven nets stay
  **unqualified**, so the qualification is not applied over-broadly.

The expected strings are not guesses: every one of them was read back from
``kicad-cli sch export netlist`` (KiCad 10.0.6) on these exact fixtures, and
:func:`test_kicad_cli_agrees_with_our_qualified_identities` re-derives them
from ``kicad-cli`` at test time wherever it is installed.

Neighbouring files: ``test_board_lvs_hierarchical.py`` covers the *recursion*
into sub-sheets (issue #4099, which made LVS vacuous); this file covers the
*naming* of what that recursion finds.  ``test_board_lvs_placeholder_nets.py``
covers the sibling tolerance for auto-generated ``Net-(...)`` names (#4615).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.lvs.board_lvs import (
    _pcb_pin_to_net,
    _schematic_pin_to_net,
    _walk_hierarchy_schematics,
    compare_netlists,
)
from kicad_tools.schematic.models.schematic import Schematic

_FIXTURES = Path(__file__).parent / "fixtures" / "sheet_qualified_lvs"
_COLLISION_SCH = _FIXTURES / "collision_root.kicad_sch"
_COLLISION_PCB = _FIXTURES / "collision_board.kicad_pcb"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _write_repro_schematic(dest_dir: Path) -> Path:
    """Write the issue #5809 reproduction schematic.

    Verbatim from the issue body: two resistors whose pins carry the plain
    local labels ``SENSE`` (pin 1) and ``RETURN`` (pin 2) on the root sheet.
    """
    sch = Schematic(title="Root net naming reproduction", project_name="repro")
    for ref, x in (("R1", 50.0), ("R2", 80.0)):
        r = sch.add_symbol(
            "Device:R",
            x=x,
            y=50,
            ref=ref,
            value="10k",
            footprint="Resistor_SMD:R_0805_2012Metric",
        )
        for pin, name in (("1", "SENSE"), ("2", "RETURN")):
            px, py = r.pin_position(pin)
            sch.add_wire((px, py), (px + 5, py))
            sch.add_label(name, px + 5, py)
    path = dest_dir / "repro.kicad_sch"
    sch.write(path)
    return path


_PCB_HEADER = """(kicad_pcb
  (version 20240108)
  (generator "kicadtools_test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
"""


def _write_pcb(dest: Path, bindings: dict[tuple[str, str], str]) -> Path:
    """Write a minimal two-pad-per-footprint PCB with the given pad nets.

    ``bindings`` maps ``(ref, pad)`` to the net *name* exactly as it should
    appear in the ``(net K "NAME")`` node, which is the whole point of these
    tests: the same node is spelled ``/SENSE`` by KiCad and ``SENSE`` by
    kicad-tools' pure-Python netlist fallback.
    """
    names = sorted(set(bindings.values()))
    codes = {name: i for i, name in enumerate(names, start=1)}

    lines = [_PCB_HEADER.rstrip("\n")]
    for name in names:
        lines.append(f'  (net {codes[name]} "{name}")')

    refs = sorted({ref for ref, _pad in bindings})
    for i, ref in enumerate(refs):
        lines.append('  (footprint "Resistor_SMD:R_0805_2012Metric"')
        lines.append('    (layer "F.Cu")')
        lines.append(f'    (uuid "sq-fp-{ref}")')
        lines.append(f"    (at {100 + 10 * i} 100 0)")
        lines.append(
            f'    (property "Reference" "{ref}" (at 0 -1.5 0) '
            f'(layer "F.SilkS") (uuid "sq-fp-{ref}-ref"))'
        )
        for pad in sorted(pad for r, pad in bindings if r == ref):
            name = bindings[(ref, pad)]
            offset = -1 if pad == "1" else 1
            lines.append(
                f'    (pad "{pad}" smd roundrect (at {offset} 0) (size 0.6 0.6) '
                f'(layers "F.Cu" "F.Paste" "F.Mask")'
            )
            lines.append(f'      (roundrect_rratio 0.25) (net {codes[name]} "{name}"))')
        lines.append("  )")
    lines.append(")")
    dest.write_text("\n".join(lines) + "\n")
    return dest


def _kicad_cli_net_names(sch_path: Path, tmp_path: Path) -> dict[tuple[str, str], str]:
    """Return ``{(ref, pin) -> net name}`` as *KiCad itself* names them."""
    import subprocess

    from kicad_tools.cli.runner import find_kicad_cli
    from kicad_tools.operations.netlist import Netlist

    cli = find_kicad_cli()
    assert cli is not None, "caller must skip when kicad-cli is absent"
    out = tmp_path / "kicad.kicad_net"
    subprocess.run(
        [
            str(cli),
            "sch",
            "export",
            "netlist",
            "--format",
            "kicadsexpr",
            "-o",
            str(out),
            str(sch_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    netlist = Netlist.load(out)
    return {(node.reference, node.pin): net.name for net in netlist.nets for node in net.nodes}


# --------------------------------------------------------------------------
# The issue's own reproduction
# --------------------------------------------------------------------------


def test_root_sheet_local_labels_resolve_to_kicad_qualified_identities(tmp_path: Path) -> None:
    """``SENSE`` on the root sheet is the net ``/SENSE``, as KiCad names it."""
    sch_path = _write_repro_schematic(tmp_path)

    assert _schematic_pin_to_net(sch_path) == {
        ("R1", "1"): "/SENSE",
        ("R1", "2"): "/RETURN",
        ("R2", "1"): "/SENSE",
        ("R2", "2"): "/RETURN",
    }


def test_repro_is_clean_against_kicad_spelled_board(tmp_path: Path) -> None:
    """The reproduction reports **zero** mismatches (issue #5809's acceptance).

    The PCB carries exactly the bindings ``kct create-pcb`` produced in the
    issue body — ``/SENSE`` and ``/RETURN``, because the netlist came from
    ``kicad-cli``.  Before the fix all four pads mismatched (``SENSE`` vs
    ``/SENSE``, ``RETURN`` vs ``/RETURN``).
    """
    sch_path = _write_repro_schematic(tmp_path)
    pcb_path = _write_pcb(
        tmp_path / "repro.kicad_pcb",
        {
            ("R1", "1"): "/SENSE",
            ("R1", "2"): "/RETURN",
            ("R2", "1"): "/SENSE",
            ("R2", "2"): "/RETURN",
        },
    )
    # Guard the fixture itself: these are the strings the issue observed.
    assert _pcb_pin_to_net(pcb_path) == {
        ("R1", "1"): "/SENSE",
        ("R1", "2"): "/RETURN",
        ("R2", "1"): "/SENSE",
        ("R2", "2"): "/RETURN",
    }

    result = compare_netlists(sch_path, pcb_path)

    assert result.mismatches == ()
    assert result.clean is True
    assert result.vacuous is False


def test_repro_is_clean_against_unqualified_board(tmp_path: Path) -> None:
    """A board spelled the *kicad-tools* way is still clean.

    kicad-tools' pure-Python netlist fallback (used when ``kicad-cli`` is
    unavailable) writes the same node as bare ``SENSE``.  Both spellings name
    one net, so both must compare clean — qualifying the schematic side must
    not break the boards kicad-tools generates for itself (the committed
    ``boards/00-simple-led`` artifacts are of this kind).
    """
    sch_path = _write_repro_schematic(tmp_path)
    pcb_path = _write_pcb(
        tmp_path / "bare.kicad_pcb",
        {
            ("R1", "1"): "SENSE",
            ("R1", "2"): "RETURN",
            ("R2", "1"): "SENSE",
            ("R2", "2"): "RETURN",
        },
    )

    result = compare_netlists(sch_path, pcb_path)

    assert result.mismatches == ()
    assert result.clean is True


@pytest.mark.parametrize("pcb_spelling", ["/{name}", "{name}"])
def test_negative_control_swapped_pad_nets_still_mismatch(
    tmp_path: Path, pcb_spelling: str
) -> None:
    """Swapping the PCB's pad nets must still be reported (both spellings).

    This is the control issue #5809 asks for: it proves the fix changed the
    net *identity* rather than relaxing the comparison.  R1's two pads are
    crossed on the board; the leaf names still differ, so the swap is caught
    whichever spelling convention the board uses.
    """
    sch_path = _write_repro_schematic(tmp_path)
    pcb_path = _write_pcb(
        tmp_path / "swapped.kicad_pcb",
        {
            # R1 crossed ...
            ("R1", "1"): pcb_spelling.format(name="RETURN"),
            ("R1", "2"): pcb_spelling.format(name="SENSE"),
            # ... R2 left correct, so the failure is localized.
            ("R2", "1"): pcb_spelling.format(name="SENSE"),
            ("R2", "2"): pcb_spelling.format(name="RETURN"),
        },
    )

    result = compare_netlists(sch_path, pcb_path)

    assert result.clean is False
    assert {(m.ref, m.pad) for m in result.mismatches} == {("R1", "1"), ("R1", "2")}
    by_key = {(m.ref, m.pad): m for m in result.mismatches}
    assert by_key[("R1", "1")].schematic_net == "/SENSE"
    assert by_key[("R1", "1")].pcb_net == pcb_spelling.format(name="RETURN")
    assert by_key[("R1", "2")].schematic_net == "/RETURN"
    assert by_key[("R1", "2")].pcb_net == pcb_spelling.format(name="SENSE")


def test_root_qualified_and_child_qualified_names_are_different_nets(tmp_path: Path) -> None:
    """``/SENSE`` (root) never satisfies a board that says ``/ChildA/SENSE``.

    Two *different* qualifications are two different nets in KiCad, so the
    tolerance for the bare spelling must not extend to them.
    """
    sch_path = _write_repro_schematic(tmp_path)
    pcb_path = _write_pcb(
        tmp_path / "wrong_sheet.kicad_pcb",
        {
            ("R1", "1"): "/ChildA/SENSE",
            ("R1", "2"): "/RETURN",
            ("R2", "1"): "/SENSE",
            ("R2", "2"): "/RETURN",
        },
    )

    result = compare_netlists(sch_path, pcb_path)

    assert result.clean is False
    assert [(m.ref, m.pad, m.schematic_net, m.pcb_net) for m in result.mismatches] == [
        ("R1", "1", "/SENSE", "/ChildA/SENSE")
    ]


# --------------------------------------------------------------------------
# Hierarchical name collisions (issue #5815's criterion, same code path)
# --------------------------------------------------------------------------


def test_same_bare_label_on_three_sheets_stays_three_identities() -> None:
    """Root, ``ChildA`` and ``ChildB`` each label a net ``SENSE``.

    All three must remain distinct nets.  ``kicad-cli`` names them
    ``/SENSE``, ``/ChildA/SENSE`` and ``/ChildB/SENSE``; the shared ``GND``
    power net stays unqualified.  Before the fix all three answered the same
    bare string ``SENSE``, which conflated three separate nets into one
    schematic identity — a false *pass* risk for copper LVS as well as the
    false mismatches of issue #5809.
    """
    pin_map = _schematic_pin_to_net(_COLLISION_SCH)

    assert pin_map == {
        ("R1", "1"): "/SENSE",
        ("R1", "2"): "GND",
        ("R3", "1"): "/ChildA/SENSE",
        ("R3", "2"): "GND",
        ("R4", "1"): "/ChildB/SENSE",
        ("R4", "2"): "GND",
    }
    sense_nets = {pin_map[key] for key in (("R1", "1"), ("R3", "1"), ("R4", "1"))}
    assert len(sense_nets) == 3


def test_hierarchical_collision_board_is_clean() -> None:
    """The board that spells all three nets the KiCad way compares clean."""
    result = compare_netlists(_COLLISION_SCH, _COLLISION_PCB)

    assert result.mismatches == ()
    assert result.clean is True


def test_collapsing_three_sibling_nets_onto_one_bare_name_still_fails(tmp_path: Path) -> None:
    """The bare-spelling tolerance must not merge sibling sheets' nets.

    A board that binds the root's, ``ChildA``'s and ``ChildB``'s ``SENSE``
    pads to a single bare ``SENSE`` net has genuinely shorted three nets.
    Three schematic identities share the leaf ``SENSE``, so the bare
    spelling names no single net and no alias is granted: every affected
    pad is reported.
    """
    text = _COLLISION_PCB.read_text()
    collapsed = tmp_path / "collapsed.kicad_pcb"
    collapsed.write_text(
        text.replace('"/ChildA/SENSE"', '"SENSE"')
        .replace('"/ChildB/SENSE"', '"SENSE"')
        .replace('"/SENSE"', '"SENSE"')
    )

    result = compare_netlists(_COLLISION_SCH, collapsed)

    assert result.clean is False
    assert {(m.ref, m.pad) for m in result.mismatches} == {
        ("R1", "1"),
        ("R3", "1"),
        ("R4", "1"),
    }
    assert all(m.pcb_net == "SENSE" for m in result.mismatches)


def _stage_global_vs_local_collision(dest_dir: Path) -> Path:
    """Copy the collision hierarchy, turning the root's ``SENSE`` global.

    Leaves exactly one *local* owner of the leaf ``SENSE``
    (``/ChildA/SENSE``) while a **global** net is also literally named
    ``SENSE`` on the root sheet — the shape that must not be collapsed.
    """
    for name in (
        "collision_root.kicad_sch",
        "collision_child_a.kicad_sch",
        "collision_child_b.kicad_sch",
    ):
        (dest_dir / name).write_text((_FIXTURES / name).read_text())
    root = dest_dir / "collision_root.kicad_sch"
    root.write_text(root.read_text().replace('(label "SENSE"', '(global_label "SENSE"'))
    child_b = dest_dir / "collision_child_b.kicad_sch"
    child_b.write_text(child_b.read_text().replace('(label "SENSE"', '(label "OTHER"'))
    return root


def test_bare_name_that_is_a_global_net_is_never_an_alias(tmp_path: Path) -> None:
    """A bare ``SENSE`` cannot stand in for ``/ChildA/SENSE`` here.

    The design has a *global* net literally named ``SENSE`` (on the root
    sheet) alongside ``ChildA``'s local ``/ChildA/SENSE``.  A board binding
    ``ChildA``'s pad to bare ``SENSE`` has put it on the global net — a real
    error — so the alias is withheld even though only one *local* net owns
    the leaf.
    """
    root = _stage_global_vs_local_collision(tmp_path)

    # Precondition: one local owner of the leaf, plus a bare global net.
    assert _schematic_pin_to_net(root) == {
        ("R1", "1"): "SENSE",
        ("R1", "2"): "GND",
        ("R3", "1"): "/ChildA/SENSE",
        ("R3", "2"): "GND",
        ("R4", "1"): "/ChildB/OTHER",
        ("R4", "2"): "GND",
    }

    pcb = _write_pcb(
        tmp_path / "board.kicad_pcb",
        {
            ("R1", "1"): "SENSE",
            ("R1", "2"): "GND",
            ("R3", "1"): "SENSE",  # <- should have been /ChildA/SENSE
            ("R3", "2"): "GND",
            ("R4", "1"): "/ChildB/OTHER",
            ("R4", "2"): "GND",
        },
    )

    result = compare_netlists(root, pcb)

    assert result.clean is False
    assert [(m.ref, m.pad, m.schematic_net, m.pcb_net) for m in result.mismatches] == [
        ("R3", "1", "/ChildA/SENSE", "SENSE")
    ]


def test_board_that_uses_both_spellings_keeps_them_distinct(tmp_path: Path) -> None:
    """A board carrying both ``/SENSE`` and ``SENSE`` is taken at its word.

    Using both spellings is the board stating that these are two nets — so
    the reproduction's two-pad ``SENSE`` net, split across the qualified and
    the bare spelling on the board, is a genuine open that must be reported.
    Granting the alias here would hide it.
    """
    sch_path = _write_repro_schematic(tmp_path)
    # R1.1 and R2.1 are one net (``/SENSE``) in the schematic; the board
    # puts them on two differently-spelled nets.
    pcb = _write_pcb(
        tmp_path / "split.kicad_pcb",
        {
            ("R1", "1"): "/SENSE",
            ("R1", "2"): "/RETURN",
            ("R2", "1"): "SENSE",
            ("R2", "2"): "/RETURN",
        },
    )

    result = compare_netlists(sch_path, pcb)

    assert result.clean is False
    assert [(m.ref, m.pad, m.schematic_net, m.pcb_net) for m in result.mismatches] == [
        ("R2", "1", "/SENSE", "SENSE")
    ]


def test_walk_yields_sheet_name_paths_not_filenames() -> None:
    """The walker's sheet path is built from ``Sheetname``, not ``Sheetfile``.

    KiCad's net names use the sheet *name* (``/ChildA/SENSE``); the fixture's
    filenames (``collision_child_a.kicad_sch``) deliberately differ from the
    sheet names so a filename-based path would be visible here.
    """
    paths = [visit.sheet_path for visit in _walk_hierarchy_schematics(_COLLISION_SCH)]

    assert paths == ["", "/ChildA", "/ChildB"]


# --------------------------------------------------------------------------
# Regression guard: non-local names stay unqualified
# --------------------------------------------------------------------------


def test_global_label_and_power_nets_are_not_qualified(tmp_path: Path) -> None:
    """Global labels, power symbols and ``PWR_FLAG`` nets keep bare names.

    Only *local* labels are sheet-scoped.  Qualifying a global label would
    break exactly the nets that are meant to span sheets, and qualifying a
    power net would make ``GND``/``+3V3`` mismatch on every board.
    """
    sch = Schematic(title="Unqualified name sources", project_name="guard")
    r1 = sch.add_symbol(
        "Device:R", x=50, y=50, ref="R1", value="10k", footprint="Resistor_SMD:R_0805_2012Metric"
    )
    # Pin 1 -> global label; pin 2 -> GND power symbol carrying a PWR_FLAG.
    px, py = r1.pin_position("1")
    sch.add_wire((px, py), (px, py - 5))
    sch.add_global_label("VBUS_GLOBAL", px, py - 5)

    px2, py2 = r1.pin_position("2")
    sch.add_wire((px2, py2), (px2, py2 + 5))
    sch.add_power("power:GND", px2, py2 + 5)
    sch.add_pwr_flag(px2, py2 + 5)

    # A third pin-bearing part gives the local-label case something to
    # contrast against on the same sheet.
    r2 = sch.add_symbol(
        "Device:R", x=90, y=50, ref="R2", value="1k", footprint="Resistor_SMD:R_0805_2012Metric"
    )
    lx, ly = r2.pin_position("1")
    sch.add_wire((lx, ly), (lx, ly - 5))
    sch.add_label("LOCAL_ONLY", lx, ly - 5)

    sch_path = tmp_path / "guard.kicad_sch"
    sch.write(sch_path)

    pin_map = _schematic_pin_to_net(sch_path)

    assert pin_map[("R1", "1")] == "VBUS_GLOBAL", "global label must not be sheet-qualified"
    assert pin_map[("R1", "2")] == "GND", "power-symbol net must not be sheet-qualified"
    # ... while the plain local label on the very same sheet *is* qualified.
    assert pin_map[("R2", "1")] == "/LOCAL_ONLY"


def test_hierarchical_label_nets_stay_unqualified() -> None:
    """Sheet-pin / hierarchical-label nets keep their previous bare spelling.

    A sheet-pin net's KiCad name comes from wherever the net sits *highest*
    in the hierarchy — a parent's local label wins over the child's
    hierarchical label — which a per-sheet resolution cannot determine.
    Rather than invent a different wrong answer, these are left exactly as
    they were before issue #5809 (cross-sheet unification is #4099 Phase 2).
    """
    root = Path(__file__).parent / "fixtures" / "hierarchical_lvs" / "root_lvs.kicad_sch"
    # This fixture's sub-sheets name their nets with power symbols, which are
    # unqualified for the same reason; the point here is that recursing into
    # a sub-sheet does not by itself add a qualification.
    assert _schematic_pin_to_net(root) == {
        ("R1", "1"): "VCC",
        ("R1", "2"): "GND",
        ("R2", "1"): "VCC",
        ("R2", "2"): "GND",
    }


def test_unnamed_net_placeholders_are_not_qualified(tmp_path: Path) -> None:
    """Auto-generated ``Net-(...)`` identities are left untouched.

    Their cross-tool spelling is reconciled by the placeholder partition
    comparison (#4615); prefixing them would only invent a third spelling.
    """
    sch = Schematic(title="Unnamed node", project_name="unnamed")
    r1 = sch.add_symbol(
        "Device:R", x=50, y=50, ref="R1", value="10k", footprint="Resistor_SMD:R_0805_2012Metric"
    )
    r2 = sch.add_symbol(
        "Device:R", x=50, y=70, ref="R2", value="1k", footprint="Resistor_SMD:R_0805_2012Metric"
    )
    # Join R1.2 to R2.1 with no label at all -> unnamed net.
    p1 = r1.pin_position("2")
    p2 = r2.pin_position("1")
    sch.add_wire(p1, p2)
    sch_path = tmp_path / "unnamed.kicad_sch"
    sch.write(sch_path)

    pin_map = _schematic_pin_to_net(sch_path)

    joined = pin_map[("R1", "2")]
    assert joined is not None
    assert joined.startswith("Net-("), joined
    assert pin_map[("R2", "1")] == joined


# --------------------------------------------------------------------------
# Cross-check against KiCad itself
# --------------------------------------------------------------------------


def _kicad_cli_or_skip():
    from kicad_tools.cli.runner import find_kicad_cli

    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not installed; cross-tool check skipped")
    return cli


def test_kicad_cli_agrees_with_our_qualified_identities(tmp_path: Path) -> None:
    """``kicad-cli``'s own netlist names must equal the identities we report.

    This is the load-bearing claim of the fix — the schematic side now
    speaks KiCad's net-naming language — so it is checked against KiCad
    rather than against a hand-written expectation, on both the root-sheet
    reproduction and the three-sheet collision fixture.
    """
    _kicad_cli_or_skip()

    repro = _write_repro_schematic(tmp_path)
    assert _kicad_cli_net_names(repro, tmp_path) == {
        ("R1", "1"): "/SENSE",
        ("R1", "2"): "/RETURN",
        ("R2", "1"): "/SENSE",
        ("R2", "2"): "/RETURN",
    }
    assert _schematic_pin_to_net(repro) == _kicad_cli_net_names(repro, tmp_path)

    # The hierarchical fixture is read from the fixture directory (kicad-cli
    # resolves sub-sheets relative to the root sheet, so it must stay put).
    kicad_names = _kicad_cli_net_names(_COLLISION_SCH, tmp_path)
    assert kicad_names == {
        ("R1", "1"): "/SENSE",
        ("R1", "2"): "GND",
        ("R3", "1"): "/ChildA/SENSE",
        ("R3", "2"): "GND",
        ("R4", "1"): "/ChildB/SENSE",
        ("R4", "2"): "GND",
    }
    assert _schematic_pin_to_net(_COLLISION_SCH) == kicad_names


def test_end_to_end_create_pcb_then_lvs_is_clean(tmp_path: Path) -> None:
    """The issue's verbatim reproduction, end to end, is clean.

    Builds the schematic, runs the same ``create-pcb`` workflow the issue
    used (which takes its netlist from ``kicad-cli``), then compares.  The
    issue reported four mismatches here; the acceptance criterion is zero.
    """
    _kicad_cli_or_skip()

    from kicad_tools.workflow import PCBFromSchematic

    sch_path = _write_repro_schematic(tmp_path)
    workflow = PCBFromSchematic(str(sch_path), netlist_path=tmp_path / "repro-netlist.kicad_net")
    workflow.create_pcb(width=100, height=80, layers=2)
    workflow.place_all_components()
    workflow.assign_nets()
    pcb_path = tmp_path / "repro.kicad_pcb"
    workflow.save(pcb_path)

    # The board really does carry KiCad's qualified spelling -- otherwise
    # this test would be proving nothing about issue #5809.
    assert set(_pcb_pin_to_net(pcb_path).values()) == {"/SENSE", "/RETURN"}

    result = compare_netlists(sch_path, pcb_path)

    assert result.mismatches == ()
    assert result.clean is True

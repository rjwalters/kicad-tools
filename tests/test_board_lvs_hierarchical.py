"""Regression tests for hierarchical-sheet recursion in board LVS (issue #4099).

`kct check`'s LVS reads the schematic through
:func:`kicad_tools.lvs.board_lvs._schematic_pin_to_net`, which used to load
only the root ``.kicad_sch`` and never follow ``(sheet ...)`` references. On a
design whose root sheet holds *only* sheet symbols (all components living in
sub-sheets — the normal organization for a non-trivial board), that root-only
load saw zero symbols, so both LVS engines bound zero pads and ran vacuously:
``bound_pads=0 / board_pads=N``, comparing an empty schematic against a
populated board. The #4011 vacuity guard correctly surfaced this as a
``bound_pad_count=0`` failure rather than a silent false pass, but the real fix
is to bind the sub-sheet pins.

These tests use a self-contained fixture pair under
``tests/fixtures/hierarchical_lvs/``:

* ``root_lvs.kicad_sch`` — only two ``(sheet ...)`` symbols, zero components;
* ``sub_mcu.kicad_sch`` — R1 wired VCC(pin1) / GND(pin2) via power symbols;
* ``sub_pwr.kicad_sch`` — R2 wired VCC(pin1) / GND(pin2) via power symbols;
* ``board_lvs.kicad_pcb`` — R1/R2 footprints on VCC/GND, routed clean.

The "before" state is asserted directly: :meth:`Schematic.load` on the root
sees no symbols, so an intentionally root-only pin map is empty (vacuous). The
"after" state is the real recursive behavior of ``_schematic_pin_to_net``.
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.lvs import VACUOUS_KIND
from kicad_tools.lvs.board_lvs import (
    _pcb_pin_to_net,
    _schematic_pin_to_net,
    compare_netlists,
)
from kicad_tools.lvs.copper_lvs import compare_copper_netlist
from kicad_tools.schematic.models import Schematic

_FIXTURES = Path(__file__).parent / "fixtures" / "hierarchical_lvs"
_ROOT_SCH = _FIXTURES / "root_lvs.kicad_sch"
_BOARD_PCB = _FIXTURES / "board_lvs.kicad_pcb"
_ROOT_FLOAT_SCH = _FIXTURES / "root_float.kicad_sch"


def test_root_sheet_alone_sees_no_symbols() -> None:
    """The 'before' state: a root-only load of this fixture is empty.

    This is the literal condition that made LVS vacuous — the root sheet
    contains only ``(sheet ...)`` symbols, so a single-file
    :meth:`Schematic.load` (which does not recurse) yields zero component
    symbols and would bind zero pads.
    """
    root = Schematic.load(str(_ROOT_SCH))
    assert [s.reference for s in root.symbols] == []


def test_schematic_pin_to_net_binds_sub_sheet_pins() -> None:
    """``_schematic_pin_to_net`` now recurses into every sub-sheet.

    Both R1 (sub_mcu) and R2 (sub_pwr) — absent from the root sheet —
    bind their pins to the label-resolved nets VCC/GND, proving the
    walker reaches sub-sheet symbols the root-only loader missed.
    """
    pin_map = _schematic_pin_to_net(_ROOT_SCH)

    assert pin_map == {
        ("R1", "1"): "VCC",
        ("R1", "2"): "GND",
        ("R2", "1"): "VCC",
        ("R2", "2"): "GND",
    }
    # All four sub-sheet pads bind to real nets — none are dropped or None.
    assert all(v is not None for v in pin_map.values())
    assert len(pin_map) == 4


def test_label_lvs_is_clean_and_non_vacuous_on_hierarchical_design() -> None:
    """``compare_netlists`` produces a real clean verdict, not a vacuous pass.

    Every board pad's declared net (VCC/GND) matches the recursively-bound
    schematic net, so the label-based LVS is genuinely clean — and, crucially,
    it compared four real pins rather than an empty schematic.
    """
    # Sanity: the board really does carry the four pads we expect.
    assert _pcb_pin_to_net(_BOARD_PCB) == {
        ("R1", "1"): "VCC",
        ("R1", "2"): "GND",
        ("R2", "1"): "VCC",
        ("R2", "2"): "GND",
    }

    result = compare_netlists(_ROOT_SCH, _BOARD_PCB)
    assert result.clean is True
    assert result.mismatches == ()


def test_copper_lvs_vacuity_guard_no_longer_fires() -> None:
    """The #4011 vacuity guard stops firing once sub-sheet pins bind.

    On ``main`` this hierarchical design produced ``bound_pad_count=0`` and a
    synthetic ``VACUOUS_KIND`` mismatch. After the fix, four pads bind and the
    copper-extracted LVS returns a genuine ``clean=True`` verdict with real
    evidence.
    """
    result = compare_copper_netlist(_ROOT_SCH, _BOARD_PCB)

    assert result.vacuous is False
    assert not any(m.kind == VACUOUS_KIND for m in result.mismatches)
    assert result.bound_pad_count == 4
    assert result.clean is True
    assert result.shorts == ()
    assert result.opens == ()


def test_copper_lvs_detects_a_real_short_in_hierarchical_design() -> None:
    """A genuine short in the wired hierarchy is now caught, not masked.

    Previously the vacuous comparison certified nothing; with real bindings,
    fusing R1's VCC pad to R2's GND pad on the copper is a detectable short.
    This mirrors the fixture but re-routes GND's segment to bridge onto the
    VCC island, proving the recursion enables real short detection.
    """
    from kicad_tools.lvs.copper_lvs import compare_partitions

    schematic_net_of_pad = _schematic_pin_to_net(_ROOT_SCH)
    # Adversarial copper: R1.1 (VCC) fused with R2.2 (GND) into one island.
    partition = [
        frozenset({"R1.1", "R2.2"}),
        frozenset({"R1.2"}),
        frozenset({"R2.1"}),
    ]
    result = compare_partitions(schematic_net_of_pad, partition)

    assert result.vacuous is False
    assert result.clean is False
    assert len(result.shorts) == 1
    short = result.shorts[0]
    assert {short.net_a, short.net_b} == {"VCC", "GND"}


def test_floating_sub_sheet_pin_resolves_to_none_not_dropped() -> None:
    """A floating pin in a sub-sheet maps to ``None``, not silently dropped.

    R3 in ``sub_float.kicad_sch`` has pin 2 wired to GND but pin 1 left
    unconnected. The unconnected pin must still appear in the map with a
    ``None`` net (matching the single-sheet ``get_net_for_pin`` convention);
    dropping it would falsely suppress a real open/short on the PCB side.
    """
    pin_map = _schematic_pin_to_net(_ROOT_FLOAT_SCH)

    assert pin_map == {
        ("R3", "1"): None,  # floating: present, explicitly None
        ("R3", "2"): "GND",
    }

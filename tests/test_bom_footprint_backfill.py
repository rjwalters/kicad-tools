"""Tests for ``backfill_footprints_from_pcb`` (Issue #3209).

The helper is a strict fallback that fills empty ``BOMItem.footprint``
fields from the PCB's footprint refs.  Items with any existing footprint
string are preserved verbatim, so schematic-driven boards (e.g. board 01)
must remain byte-identical before and after the fix.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.schema.bom import BOMItem, backfill_footprints_from_pcb

BOARDS_DIR = Path(__file__).resolve().parent.parent / "boards"

BOARD_01_PCB = BOARDS_DIR / "01-voltage-divider" / "output" / "voltage_divider_routed.kicad_pcb"
BOARD_01_SCH = BOARDS_DIR / "01-voltage-divider" / "output" / "voltage_divider.kicad_sch"

BOARD_03_PCB = BOARDS_DIR / "03-usb-joystick" / "output" / "usb_joystick_routed.kicad_pcb"
BOARD_03_SCH = BOARDS_DIR / "03-usb-joystick" / "output" / "usb_joystick.kicad_sch"


def _make_item(ref: str, value: str = "10k", footprint: str = "") -> BOMItem:
    return BOMItem(
        reference=ref,
        value=value,
        footprint=footprint,
        lib_id="Device:R",
    )


def _mock_pcb(refs_to_names: dict[str, str]) -> MagicMock:
    """Build a mock PCB with the given (reference -> footprint name) pairs."""
    fps = []
    for ref, name in refs_to_names.items():
        fp = MagicMock()
        fp.reference = ref
        fp.name = name
        fps.append(fp)
    pcb = MagicMock()
    pcb.footprints = fps
    return pcb


# ---------------------------------------------------------------------------
# Unit tests: three canonical scenarios (curator pattern)
# ---------------------------------------------------------------------------


class TestSchematicOnlyNoRegression:
    """Schematic-side metadata must be preserved (board 01 gold standard)."""

    def test_existing_footprint_preserved_even_when_pcb_differs(self):
        # Schematic provides "R_0402"; PCB carries a different name.
        # The helper must NOT overwrite the schematic-side value.
        items = [_make_item("R1", footprint="R_0402")]
        pcb = _mock_pcb({"R1": "R_0603"})

        with patch("kicad_tools.schema.pcb.PCB.load", return_value=pcb):
            filled = backfill_footprints_from_pcb(items, "/fake/path.kicad_pcb")

        assert filled == 0
        assert items[0].footprint == "R_0402"

    def test_existing_footprint_with_only_whitespace_is_treated_as_empty(self):
        # Whitespace-only footprints are considered empty and therefore
        # eligible for back-fill.
        items = [_make_item("R1", footprint="   ")]
        pcb = _mock_pcb({"R1": "R_0805"})

        with patch("kicad_tools.schema.pcb.PCB.load", return_value=pcb):
            filled = backfill_footprints_from_pcb(items, "/fake/path.kicad_pcb")

        assert filled == 1
        assert items[0].footprint == "R_0805"

    def test_returns_zero_when_nothing_to_backfill(self):
        # All items populated -> no PCB-side overrides applied.
        items = [
            _make_item("R1", footprint="R_0402"),
            _make_item("R2", footprint="R_0603"),
        ]
        pcb = _mock_pcb({"R1": "R_X", "R2": "R_Y"})

        with patch("kicad_tools.schema.pcb.PCB.load", return_value=pcb):
            filled = backfill_footprints_from_pcb(items, "/fake/path.kicad_pcb")

        assert filled == 0
        assert items[0].footprint == "R_0402"
        assert items[1].footprint == "R_0603"


class TestSpecOverlayBlank:
    """Board 03 case: schematic Footprint blank, PCB has the real name."""

    def test_blank_footprints_filled_from_pcb(self):
        items = [
            _make_item("U1", value="MCU", footprint=""),
            _make_item("J1", value="USB-C", footprint=""),
            _make_item("J2", value="Joystick", footprint=""),
            _make_item("Y1", value="16MHz", footprint=""),
            _make_item("SW1", value="Button", footprint=""),
        ]
        pcb = _mock_pcb(
            {
                "U1": "Package_QFP:TQFP-32_7x7mm_P0.8mm",
                "J1": "Connector_USB:USB_C_Receptacle",
                "J2": "Connector:Joystick_PSP",
                "Y1": "Crystal:Crystal_SMD_3225",
                "SW1": "Button_Switch_SMD:SW_SPST_6mm",
            }
        )

        with patch("kicad_tools.schema.pcb.PCB.load", return_value=pcb):
            filled = backfill_footprints_from_pcb(items, "/fake/path.kicad_pcb")

        assert filled == 5
        assert items[0].footprint == "Package_QFP:TQFP-32_7x7mm_P0.8mm"
        assert items[1].footprint == "Connector_USB:USB_C_Receptacle"
        assert items[2].footprint == "Connector:Joystick_PSP"
        assert items[3].footprint == "Crystal:Crystal_SMD_3225"
        assert items[4].footprint == "Button_Switch_SMD:SW_SPST_6mm"


class TestMixed:
    """Mixed: some refs in PCB, some not; some populated, some blank."""

    def test_only_blank_items_with_pcb_match_are_filled(self):
        items = [
            _make_item("R1", footprint=""),  # blank, no PCB match -> stays blank
            _make_item("U1", footprint=""),  # blank, PCB match -> filled
            _make_item("J1", footprint="Connector_OldName"),  # populated, PCB differs -> preserved
        ]
        pcb = _mock_pcb(
            {
                "U1": "TQFP-32",
                "J1": "Connector_NewName",
                # No entry for R1.
            }
        )

        with patch("kicad_tools.schema.pcb.PCB.load", return_value=pcb):
            filled = backfill_footprints_from_pcb(items, "/fake/path.kicad_pcb")

        assert filled == 1
        assert items[0].footprint == ""  # no PCB match, no error
        assert items[1].footprint == "TQFP-32"
        assert items[2].footprint == "Connector_OldName"  # preserved

    def test_empty_input_returns_zero(self):
        with patch("kicad_tools.schema.pcb.PCB.load", return_value=_mock_pcb({})):
            filled = backfill_footprints_from_pcb([], "/fake/path.kicad_pcb")
        assert filled == 0


# ---------------------------------------------------------------------------
# Integration tests against real boards
# ---------------------------------------------------------------------------


class TestBoard03Integration:
    """Integration test against board 03 (the AC scenario)."""

    def test_board_03_blank_footprints_filled_from_pcb(self):
        if not BOARD_03_PCB.exists() or not BOARD_03_SCH.exists():
            pytest.skip("Board 03 artifacts not found")

        from kicad_tools.schema.bom import extract_bom

        bom = extract_bom(str(BOARD_03_SCH))
        items = bom.items

        # Board 03's schematic has since been repaired upstream (the
        # spec-overlay refs now carry footprints), so synthesize the
        # original defect in-memory to keep exercising the back-fill
        # path against the real PCB (issue #3436 burn-down).
        target_refs = {"U1", "J1", "J2", "Y1", "SW1"}
        target_items = [it for it in items if it.reference in target_refs]
        assert target_items, "None of the spec-overlay refs found in BOM"
        for it in target_items:
            it.footprint = ""
        assert all(not (it.footprint or "").strip() for it in target_items)

        filled = backfill_footprints_from_pcb(items, str(BOARD_03_PCB))
        assert filled > 0

        # After back-fill: AC #1 — U1/J1/J2/Y1/SW1 must be non-blank.
        for it in target_items:
            assert (it.footprint or "").strip(), (
                f"Footprint still blank for {it.reference} after back-fill"
            )

        # AC #2: every BOM row (caps, buttons too) should now have a
        # populated footprint, because the PCB carries footprint refs for
        # every placed component.
        for it in items:
            assert (it.footprint or "").strip(), (
                f"Footprint still blank for {it.reference} after back-fill"
            )


class TestBoard01NoRegression:
    """Board 01 is the gold-standard schematic-source board (AC #4)."""

    def test_board_01_bom_unchanged_after_backfill(self):
        if not BOARD_01_PCB.exists() or not BOARD_01_SCH.exists():
            pytest.skip("Board 01 artifacts not found")

        from kicad_tools.schema.bom import extract_bom

        bom = extract_bom(str(BOARD_01_SCH))
        items = bom.items

        # Board 01 schematic populates Footprint for every *placed*
        # instance. Virtual items (power symbols like #PWR01 for the
        # synthesized VIN rail introduced by #3291) are global net
        # labels, not real footprints, so they're excluded from the
        # precondition.
        real_items = [it for it in items if not it.is_virtual]
        assert real_items, "Precondition: board 01 must have non-virtual BOM items"
        assert all((it.footprint or "").strip() for it in real_items), (
            "Precondition: board 01 schematic should populate all footprints for non-virtual items"
        )

        snapshot = [(it.reference, it.footprint) for it in items]
        filled = backfill_footprints_from_pcb(items, str(BOARD_01_PCB))

        # No back-fill should occur and no field should change.
        assert filled == 0
        after = [(it.reference, it.footprint) for it in items]
        assert after == snapshot

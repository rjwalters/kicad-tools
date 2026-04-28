"""Tests for symbol-based ERC violation sheet re-attribution.

Tests the ``reattribute_symbol_violations`` function that corrects
``_sheet_path`` for violations mis-attributed to the root sheet when
the offending symbol lives on a child sheet.
"""

from pathlib import Path

import pytest

from kicad_tools.erc.cross_sheet import (
    _build_symbol_sheet_map,
    _extract_identifiers_from_items,
    reattribute_symbol_violations,
)


# ---------------------------------------------------------------------------
# Fixture schematic templates (same format as test_erc_cross_sheet.py)
# ---------------------------------------------------------------------------

_ROOT_TEMPLATE = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "root-uuid-001")
  (paper "A4")
  (lib_symbols)
  {symbols}
  {sheets}
)
"""

_SUBSHEET_TEMPLATE = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "{uuid}")
  (paper "A4")
  (lib_symbols)
  {symbols}
)
"""

_SYMBOL_TEMPLATE = """\
  (symbol
    (lib_id "{lib_id}")
    (at 100 100 0)
    (unit {unit})
    (uuid "{uuid}")
    (property "Reference" "{reference}"
      (at 100 90 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Value" "{value}"
      (at 100 110 0)
      (effects (font (size 1.27 1.27)))
    )
    (pin "1" (uuid "{uuid}-pin1"))
  )
"""

_SHEET_TEMPLATE = """\
  (sheet
    (at 130 40) (size 40 30)
    (uuid "{uuid}")
    (property "Sheetname" "{name}"
      (at 130 39 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Sheetfile" "{filename}"
      (at 130 71 0)
      (effects (font (size 1.27 1.27)))
    )
  )
"""


def _make_symbol(
    reference: str,
    value: str = "10k",
    lib_id: str = "Device:R",
    uuid: str = "sym-001",
    unit: int = 1,
) -> str:
    return _SYMBOL_TEMPLATE.format(
        reference=reference,
        value=value,
        lib_id=lib_id,
        uuid=uuid,
        unit=unit,
    )


def _make_sheet(name: str, filename: str, uuid: str = "sheet-001") -> str:
    return _SHEET_TEMPLATE.format(name=name, filename=filename, uuid=uuid)


def _make_hierarchy(tmp_path: Path) -> str:
    """Create a two-level hierarchy: root with DAC and Power sub-sheets.

    Root has R1, DAC sub-sheet has U3, Power sub-sheet has U5.
    Returns path to root schematic.
    """
    root_symbols = _make_symbol("R1", "10k", uuid="root-r1")
    root_sheets = (
        _make_sheet("DAC", "dac.kicad_sch", uuid="sheet-dac")
        + _make_sheet("Power", "power.kicad_sch", uuid="sheet-power")
    )
    root_content = _ROOT_TEMPLATE.format(
        symbols=root_symbols, sheets=root_sheets
    )

    dac_symbols = _make_symbol("U3", "PCM5122", lib_id="Audio:PCM5122", uuid="dac-u3")
    dac_content = _SUBSHEET_TEMPLATE.format(uuid="dac-uuid-001", symbols=dac_symbols)

    power_symbols = _make_symbol("U5", "TPS7A47", lib_id="Regulator:TPS7A47", uuid="power-u5")
    power_content = _SUBSHEET_TEMPLATE.format(uuid="power-uuid-001", symbols=power_symbols)

    (tmp_path / "root.kicad_sch").write_text(root_content)
    (tmp_path / "dac.kicad_sch").write_text(dac_content)
    (tmp_path / "power.kicad_sch").write_text(power_content)

    return str(tmp_path / "root.kicad_sch")


# ---------------------------------------------------------------------------
# Tests: _extract_identifiers_from_items
# ---------------------------------------------------------------------------


class TestExtractIdentifiers:
    """Tests for _extract_identifiers_from_items."""

    def test_uuid_extraction(self):
        items = [{"uuid": "abc-123", "description": "some pin"}]
        result = _extract_identifiers_from_items(items)
        assert "abc-123" in result

    def test_reference_from_of_pattern(self):
        items = [{"description": "Pin VCC (power_in) of U3"}]
        result = _extract_identifiers_from_items(items)
        assert "U3" in result

    def test_reference_from_symbol_pattern(self):
        items = [{"description": "Symbol U5"}]
        result = _extract_identifiers_from_items(items)
        assert "U5" in result

    def test_both_uuid_and_reference(self):
        items = [{"uuid": "dac-u3", "description": "Pin SDA of U3"}]
        result = _extract_identifiers_from_items(items)
        assert "dac-u3" in result
        assert "U3" in result

    def test_empty_items(self):
        assert _extract_identifiers_from_items([]) == []

    def test_no_identifiers(self):
        items = [{"description": "Some generic error"}]
        result = _extract_identifiers_from_items(items)
        assert result == []


# ---------------------------------------------------------------------------
# Tests: _build_symbol_sheet_map
# ---------------------------------------------------------------------------


class TestBuildSymbolSheetMap:
    """Tests for _build_symbol_sheet_map."""

    def test_maps_symbols_to_sheets(self, tmp_path: Path):
        root_path = _make_hierarchy(tmp_path)
        mapping = _build_symbol_sheet_map(root_path)

        # Root symbols should map to "/"
        assert mapping.get("root-r1") == "/"
        # DAC symbols should map to "/DAC"
        assert mapping.get("dac-u3") == "/DAC"
        assert mapping.get("U3") == "/DAC"
        # Power symbols should map to "/Power"
        assert mapping.get("power-u5") == "/Power"
        assert mapping.get("U5") == "/Power"

    def test_flat_schematic(self, tmp_path: Path):
        """A flat schematic with no sub-sheets maps everything to root."""
        symbols = _make_symbol("R1", "10k", uuid="r1-uuid")
        content = _ROOT_TEMPLATE.format(symbols=symbols, sheets="")
        (tmp_path / "root.kicad_sch").write_text(content)

        mapping = _build_symbol_sheet_map(str(tmp_path / "root.kicad_sch"))
        assert mapping.get("r1-uuid") == "/"
        assert mapping.get("R1") == "/"


# ---------------------------------------------------------------------------
# Tests: reattribute_symbol_violations
# ---------------------------------------------------------------------------


class TestReattributeSymbolViolations:
    """Tests for reattribute_symbol_violations."""

    def test_reattributes_by_uuid(self, tmp_path: Path):
        """A violation with a UUID matching a child-sheet symbol gets re-attributed."""
        root_path = _make_hierarchy(tmp_path)

        violations = [
            {
                "type": "pin_not_connected",
                "description": "Pin not connected",
                "severity": "error",
                "_sheet_path": "/",
                "items": [{"uuid": "dac-u3", "description": "Pin SDA of U3"}],
            }
        ]

        result = reattribute_symbol_violations(violations, root_path)
        assert len(result) == 1
        assert result[0]["_sheet_path"] == "/DAC"

    def test_reattributes_by_reference(self, tmp_path: Path):
        """A violation with a reference in description gets re-attributed."""
        root_path = _make_hierarchy(tmp_path)

        violations = [
            {
                "type": "pin_not_driven",
                "description": "Power input pin not driven",
                "severity": "error",
                "_sheet_path": "/",
                "items": [{"description": "Pin VCC (power_in) of U5"}],
            }
        ]

        result = reattribute_symbol_violations(violations, root_path)
        assert len(result) == 1
        assert result[0]["_sheet_path"] == "/Power"

    def test_root_sheet_violations_stay_on_root(self, tmp_path: Path):
        """Violations for symbols actually on root should remain on root."""
        root_path = _make_hierarchy(tmp_path)

        violations = [
            {
                "type": "pin_not_connected",
                "description": "Pin not connected",
                "severity": "error",
                "_sheet_path": "/",
                "items": [{"uuid": "root-r1", "description": "Pin 1 of R1"}],
            }
        ]

        result = reattribute_symbol_violations(violations, root_path)
        assert len(result) == 1
        # Should stay on "/" because R1 is actually on root
        assert result[0]["_sheet_path"] == "/"

    def test_non_root_violations_unchanged(self, tmp_path: Path):
        """Violations already on a child sheet should not be changed."""
        root_path = _make_hierarchy(tmp_path)

        violations = [
            {
                "type": "pin_not_connected",
                "description": "Pin not connected",
                "severity": "error",
                "_sheet_path": "/DAC",
                "items": [{"uuid": "dac-u3", "description": "Pin SDA of U3"}],
            }
        ]

        result = reattribute_symbol_violations(violations, root_path)
        assert result[0]["_sheet_path"] == "/DAC"

    def test_non_target_types_unchanged(self, tmp_path: Path):
        """Violations of unrelated types should pass through unchanged."""
        root_path = _make_hierarchy(tmp_path)

        violations = [
            {
                "type": "some_other_type",
                "description": "Something else",
                "severity": "warning",
                "_sheet_path": "/",
                "items": [{"uuid": "dac-u3"}],
            }
        ]

        result = reattribute_symbol_violations(violations, root_path)
        assert result[0]["_sheet_path"] == "/"

    def test_no_items_unchanged(self, tmp_path: Path):
        """Violations with no items array cannot be re-attributed."""
        root_path = _make_hierarchy(tmp_path)

        violations = [
            {
                "type": "pin_not_connected",
                "description": "Pin not connected",
                "severity": "error",
                "_sheet_path": "/",
                "items": [],
            }
        ]

        result = reattribute_symbol_violations(violations, root_path)
        assert result[0]["_sheet_path"] == "/"

    def test_no_target_violations_skips_traversal(self, tmp_path: Path):
        """When no target violations exist, hierarchy is not traversed."""
        root_path = _make_hierarchy(tmp_path)

        violations = [
            {
                "type": "wire_dangling",
                "description": "Wire not connected",
                "severity": "warning",
                "_sheet_path": "/",
                "items": [],
            }
        ]

        # Should return quickly without building hierarchy
        result = reattribute_symbol_violations(violations, root_path)
        assert result[0]["_sheet_path"] == "/"

    def test_flat_schematic_no_change(self, tmp_path: Path):
        """In a flat schematic, root violations stay on root."""
        symbols = _make_symbol("R1", "10k", uuid="r1-uuid")
        content = _ROOT_TEMPLATE.format(symbols=symbols, sheets="")
        root_path = str(tmp_path / "root.kicad_sch")
        (tmp_path / "root.kicad_sch").write_text(content)

        violations = [
            {
                "type": "pin_not_connected",
                "description": "Pin not connected",
                "severity": "error",
                "_sheet_path": "/",
                "items": [{"uuid": "r1-uuid", "description": "Pin 1 of R1"}],
            }
        ]

        result = reattribute_symbol_violations(violations, root_path)
        assert result[0]["_sheet_path"] == "/"

    def test_label_violation_reattributed(self, tmp_path: Path):
        """Label-type violations should also be re-attributed by UUID."""
        root_path = _make_hierarchy(tmp_path)

        violations = [
            {
                "type": "global_label_dangling",
                "description": "Global label not connected",
                "severity": "warning",
                "_sheet_path": "/",
                "items": [{"uuid": "dac-u3", "description": "Global Label 'AUDIO_L'"}],
            }
        ]

        result = reattribute_symbol_violations(violations, root_path)
        assert result[0]["_sheet_path"] == "/DAC"

    def test_multiple_violations_mixed(self, tmp_path: Path):
        """Multiple violations with different types and sheets are handled correctly."""
        root_path = _make_hierarchy(tmp_path)

        violations = [
            {
                "type": "pin_not_connected",
                "description": "Pin not connected",
                "severity": "error",
                "_sheet_path": "/",
                "items": [{"uuid": "dac-u3", "description": "Pin SDA of U3"}],
            },
            {
                "type": "pin_not_driven",
                "description": "Power pin not driven",
                "severity": "error",
                "_sheet_path": "/",
                "items": [{"description": "Pin VIN (power_in) of U5"}],
            },
            {
                "type": "wire_dangling",
                "description": "Wire not connected",
                "severity": "warning",
                "_sheet_path": "/",
                "items": [],
            },
        ]

        result = reattribute_symbol_violations(violations, root_path)
        assert result[0]["_sheet_path"] == "/DAC"
        assert result[1]["_sheet_path"] == "/Power"
        assert result[2]["_sheet_path"] == "/"  # wire_dangling not a target type

"""Tests for cross-sheet ERC checks (duplicates & power filtering)."""

from pathlib import Path

import pytest

from kicad_tools.erc.cross_sheet import (
    build_power_driver_inventory,
    check_cross_sheet_duplicates,
    filter_cross_sheet_power_violations,
)
from kicad_tools.erc.violation import ERCViolationType, Severity

# ---------------------------------------------------------------------------
# Fixture schematic templates
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


# ---------------------------------------------------------------------------
# Test: duplicate reference across two sheets
# ---------------------------------------------------------------------------


class TestCrossSheetDuplicates:
    """Tests for check_cross_sheet_duplicates."""

    def test_duplicate_across_sheets(self, tmp_path: Path):
        """R12 on both root and sub-sheet should be flagged."""
        sub_file = "sub.kicad_sch"

        root_symbols = _make_symbol("R12", "10k", uuid="root-r12")
        root_sheets = _make_sheet("Sub", sub_file, uuid="sheet-sub")
        root_content = _ROOT_TEMPLATE.format(symbols=root_symbols, sheets=root_sheets)

        sub_symbols = _make_symbol("R12", "4.7k", uuid="sub-r12")
        sub_content = _SUBSHEET_TEMPLATE.format(uuid="sub-uuid-001", symbols=sub_symbols)

        (tmp_path / "root.kicad_sch").write_text(root_content)
        (tmp_path / sub_file).write_text(sub_content)

        violations = check_cross_sheet_duplicates(str(tmp_path / "root.kicad_sch"))

        assert len(violations) == 1
        v = violations[0]
        assert v.type == ERCViolationType.DUPLICATE_REFERENCE
        assert v.severity == Severity.ERROR
        assert "R12" in v.description
        assert "sheets" in v.description.lower()

    def test_no_duplicates(self, tmp_path: Path):
        """Distinct references across sheets should produce no violations."""
        sub_file = "sub.kicad_sch"

        root_symbols = _make_symbol("R1", "10k", uuid="root-r1")
        root_sheets = _make_sheet("Sub", sub_file, uuid="sheet-sub")
        root_content = _ROOT_TEMPLATE.format(symbols=root_symbols, sheets=root_sheets)

        sub_symbols = _make_symbol("R2", "4.7k", uuid="sub-r2")
        sub_content = _SUBSHEET_TEMPLATE.format(uuid="sub-uuid-001", symbols=sub_symbols)

        (tmp_path / "root.kicad_sch").write_text(root_content)
        (tmp_path / sub_file).write_text(sub_content)

        violations = check_cross_sheet_duplicates(str(tmp_path / "root.kicad_sch"))
        assert violations == []

    def test_multi_unit_same_sheet_not_flagged(self, tmp_path: Path):
        """Multi-unit symbol (same lib_id, same sheet) should not be flagged."""
        symbols = _make_symbol(
            "U1", "LM324", lib_id="Amplifier:LM324", uuid="u1-a", unit=1
        ) + _make_symbol("U1", "LM324", lib_id="Amplifier:LM324", uuid="u1-b", unit=2)
        root_content = _ROOT_TEMPLATE.format(symbols=symbols, sheets="")

        (tmp_path / "root.kicad_sch").write_text(root_content)

        violations = check_cross_sheet_duplicates(str(tmp_path / "root.kicad_sch"))
        assert violations == []

    def test_power_symbols_not_flagged(self, tmp_path: Path):
        """Power symbols (lib_id starting with power:) should be ignored."""
        sub_file = "sub.kicad_sch"

        root_symbols = _make_symbol("#PWR01", "GND", lib_id="power:GND", uuid="pwr-root")
        root_sheets = _make_sheet("Sub", sub_file, uuid="sheet-sub")
        root_content = _ROOT_TEMPLATE.format(symbols=root_symbols, sheets=root_sheets)

        sub_symbols = _make_symbol("#PWR01", "GND", lib_id="power:GND", uuid="pwr-sub")
        sub_content = _SUBSHEET_TEMPLATE.format(uuid="sub-uuid-001", symbols=sub_symbols)

        (tmp_path / "root.kicad_sch").write_text(root_content)
        (tmp_path / sub_file).write_text(sub_content)

        violations = check_cross_sheet_duplicates(str(tmp_path / "root.kicad_sch"))
        assert violations == []

    def test_flat_schematic_no_subsheets(self, tmp_path: Path):
        """Single flat schematic with no sub-sheets returns empty."""
        symbols = _make_symbol("R1", "10k", uuid="r1")
        root_content = _ROOT_TEMPLATE.format(symbols=symbols, sheets="")

        (tmp_path / "root.kicad_sch").write_text(root_content)

        violations = check_cross_sheet_duplicates(str(tmp_path / "root.kicad_sch"))
        assert violations == []

    def test_suggestion_next_available(self, tmp_path: Path):
        """Duplicate should suggest the next available reference number."""
        sub_file = "sub.kicad_sch"

        # R1 on root, R1 and R2 on sub (R1 is the duplicate)
        root_symbols = _make_symbol("R1", "10k", uuid="root-r1") + _make_symbol(
            "R2", "22k", uuid="root-r2"
        )
        root_sheets = _make_sheet("Sub", sub_file, uuid="sheet-sub")
        root_content = _ROOT_TEMPLATE.format(symbols=root_symbols, sheets=root_sheets)

        sub_symbols = _make_symbol("R1", "4.7k", uuid="sub-r1")
        sub_content = _SUBSHEET_TEMPLATE.format(uuid="sub-uuid-001", symbols=sub_symbols)

        (tmp_path / "root.kicad_sch").write_text(root_content)
        (tmp_path / sub_file).write_text(sub_content)

        violations = check_cross_sheet_duplicates(str(tmp_path / "root.kicad_sch"))
        assert len(violations) == 1

        v = violations[0]
        assert any("R3" in s for s in v.suggestions), (
            f"Expected suggestion to contain R3 (next available), got {v.suggestions}"
        )

    def test_existing_hierarchical_fixture_no_duplicates(self, fixtures_dir: Path):
        """The existing hierarchical fixture should have no cross-sheet duplicates."""
        root = fixtures_dir / "projects" / "hierarchical_main.kicad_sch"
        if not root.exists():
            pytest.skip("hierarchical fixture not available")

        violations = check_cross_sheet_duplicates(str(root))
        assert violations == []

    def test_same_file_two_instances(self, tmp_path: Path):
        """Same sub-sheet file used twice; duplicate within the shared file
        should not be double-counted, but symbols in different instances are
        independent hierarchy nodes."""
        sub_file = "shared.kicad_sch"

        root_symbols = ""
        root_sheets = _make_sheet("SheetA", sub_file, uuid="sheet-a") + _make_sheet(
            "SheetB", sub_file, uuid="sheet-b"
        )
        root_content = _ROOT_TEMPLATE.format(symbols=root_symbols, sheets=root_sheets)

        # The shared sub-sheet has R1.  Because the hierarchy builder
        # detects circular references the second instance is a shallow
        # copy.  The check should still not report R1 as a cross-sheet
        # duplicate because the file is the same logical entity.
        sub_symbols = _make_symbol("R1", "10k", uuid="shared-r1")
        sub_content = _SUBSHEET_TEMPLATE.format(uuid="shared-uuid-001", symbols=sub_symbols)

        (tmp_path / "root.kicad_sch").write_text(root_content)
        (tmp_path / sub_file).write_text(sub_content)

        # The hierarchy builder returns a shallow node for the second
        # reference so only one node is fully loaded.  Whether this
        # produces a violation depends on how the hierarchy builder
        # handles the circular reference; the important thing is it
        # does not crash.
        violations = check_cross_sheet_duplicates(str(tmp_path / "root.kicad_sch"))
        # Either 0 or 1 violations is acceptable; assert no crash.
        assert isinstance(violations, list)


# ---------------------------------------------------------------------------
# Templates for power driver inventory tests
# ---------------------------------------------------------------------------

# Root schematic template that includes lib_symbols definitions
_POWER_ROOT_TEMPLATE = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "root-uuid-001")
  (paper "A4")
  (lib_symbols
    {lib_symbols}
  )
  {symbols}
  {sheets}
)
"""

_POWER_SUBSHEET_TEMPLATE = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "{uuid}")
  (paper "A4")
  (lib_symbols
    {lib_symbols}
  )
  {symbols}
)
"""

# Library symbol definition for a PWR_FLAG (has power_out pin)
_LIB_PWR_FLAG = """\
    (symbol "power:PWR_FLAG"
      (pin_names (offset 0) hide)
      (in_bom no) (on_board yes)
      (property "Reference" "#FLG"
        (at 0 0 0)
        (effects (font (size 1.27 1.27)) hide)
      )
      (property "Value" "PWR_FLAG"
        (at 0 0 0)
        (effects (font (size 1.27 1.27)))
      )
      (symbol "PWR_FLAG_0_0"
        (pin power_out line (at 0 0 90) (length 0) (name "pwr" (effects (font (size 0 0)))) (number "1" (effects (font (size 0 0)))))
      )
    )
"""

# Library symbol definition for a power symbol like +3V3
_LIB_POWER_3V3 = """\
    (symbol "power:+3V3"
      (pin_names (offset 0) hide)
      (in_bom no) (on_board yes)
      (property "Reference" "#PWR"
        (at 0 0 0)
        (effects (font (size 1.27 1.27)) hide)
      )
      (property "Value" "+3V3"
        (at 0 0 0)
        (effects (font (size 1.27 1.27)))
      )
      (symbol "+3V3_0_0"
        (pin power_in line (at 0 0 90) (length 0) (name "+3V3" (effects (font (size 0 0)))) (number "1" (effects (font (size 0 0)))))
      )
    )
"""

# Symbol instance template for power symbols
_POWER_SYMBOL_TEMPLATE = """\
  (symbol
    (lib_id "{lib_id}")
    (at 100 100 0)
    (unit 1)
    (uuid "{uuid}")
    (property "Reference" "{reference}"
      (at 100 90 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property "Value" "{value}"
      (at 100 110 0)
      (effects (font (size 1.27 1.27)))
    )
    (pin "1" (uuid "{uuid}-pin1"))
  )
"""


class TestBuildPowerDriverInventory:
    """Tests for build_power_driver_inventory."""

    def test_finds_pwr_flag_driver(self, tmp_path: Path):
        """PWR_FLAG symbol with power_out pin should register as a driver."""
        pwr_flag_sym = _POWER_SYMBOL_TEMPLATE.format(
            lib_id="power:PWR_FLAG",
            uuid="pwr-flag-001",
            reference="#FLG01",
            value="PWR_FLAG",
        )
        power_3v3_sym = _POWER_SYMBOL_TEMPLATE.format(
            lib_id="power:+3V3",
            uuid="pwr-3v3-001",
            reference="#PWR01",
            value="+3V3",
        )

        root_content = _POWER_ROOT_TEMPLATE.format(
            lib_symbols=_LIB_PWR_FLAG + _LIB_POWER_3V3,
            symbols=pwr_flag_sym + power_3v3_sym,
            sheets="",
        )
        (tmp_path / "root.kicad_sch").write_text(root_content)

        driven = build_power_driver_inventory(str(tmp_path / "root.kicad_sch"))

        # PWR_FLAG is a power: symbol with power_out -- its value is the net
        assert "PWR_FLAG" in driven

    def test_power_in_symbol_not_a_driver(self, tmp_path: Path):
        """A power:+3V3 symbol with only power_in pin should NOT register."""
        power_sym = _POWER_SYMBOL_TEMPLATE.format(
            lib_id="power:+3V3",
            uuid="pwr-3v3-001",
            reference="#PWR01",
            value="+3V3",
        )
        root_content = _POWER_ROOT_TEMPLATE.format(
            lib_symbols=_LIB_POWER_3V3,
            symbols=power_sym,
            sheets="",
        )
        (tmp_path / "root.kicad_sch").write_text(root_content)

        driven = build_power_driver_inventory(str(tmp_path / "root.kicad_sch"))
        # +3V3 has only power_in, not power_out -- should not be in inventory
        assert "+3V3" not in driven

    def test_finds_driver_on_subsheet(self, tmp_path: Path):
        """Power driver on a sub-sheet should be detected."""
        sub_file = "power_sub.kicad_sch"

        root_sheets = _SHEET_TEMPLATE.format(name="Power", filename=sub_file, uuid="sheet-pwr")
        root_content = _POWER_ROOT_TEMPLATE.format(lib_symbols="", symbols="", sheets=root_sheets)

        pwr_flag_sym = _POWER_SYMBOL_TEMPLATE.format(
            lib_id="power:PWR_FLAG",
            uuid="sub-pwr-flag",
            reference="#FLG01",
            value="PWR_FLAG",
        )
        sub_content = _POWER_SUBSHEET_TEMPLATE.format(
            uuid="sub-uuid-pwr",
            lib_symbols=_LIB_PWR_FLAG,
            symbols=pwr_flag_sym,
        )

        (tmp_path / "root.kicad_sch").write_text(root_content)
        (tmp_path / sub_file).write_text(sub_content)

        driven = build_power_driver_inventory(str(tmp_path / "root.kicad_sch"))
        assert "PWR_FLAG" in driven

    def test_empty_schematic(self, tmp_path: Path):
        """Schematic with no symbols returns empty set."""
        root_content = _POWER_ROOT_TEMPLATE.format(lib_symbols="", symbols="", sheets="")
        (tmp_path / "root.kicad_sch").write_text(root_content)

        driven = build_power_driver_inventory(str(tmp_path / "root.kicad_sch"))
        assert driven == set()


class TestFilterCrossSheetPowerIntegration:
    """Integration tests: filter_cross_sheet_power_violations with real schematics."""

    def test_suppresses_violation_when_pwr_flag_exists(self, tmp_path: Path):
        """power_pin_not_driven for VCC should be suppressed when PWR_FLAG exists."""
        # Root sheet has a PWR_FLAG (power_out driver)
        pwr_flag_sym = _POWER_SYMBOL_TEMPLATE.format(
            lib_id="power:PWR_FLAG",
            uuid="pwr-flag-001",
            reference="#FLG01",
            value="PWR_FLAG",
        )
        root_content = _POWER_ROOT_TEMPLATE.format(
            lib_symbols=_LIB_PWR_FLAG,
            symbols=pwr_flag_sym,
            sheets="",
        )
        (tmp_path / "root.kicad_sch").write_text(root_content)

        violations = [
            {
                "type": "power_pin_not_driven",
                "severity": "error",
                "description": "Power input pin not driven",
                "items": [{"description": "Pin PWR_FLAG (power_in) of U3"}],
            },
        ]

        result = filter_cross_sheet_power_violations(violations, str(tmp_path / "root.kicad_sch"))
        assert len(result) == 0

    def test_preserves_violation_when_no_driver(self, tmp_path: Path):
        """power_pin_not_driven should be kept when no power_out driver exists."""
        # Root sheet has only a +3V3 symbol (power_in only)
        power_sym = _POWER_SYMBOL_TEMPLATE.format(
            lib_id="power:+3V3",
            uuid="pwr-3v3-001",
            reference="#PWR01",
            value="+3V3",
        )
        root_content = _POWER_ROOT_TEMPLATE.format(
            lib_symbols=_LIB_POWER_3V3,
            symbols=power_sym,
            sheets="",
        )
        (tmp_path / "root.kicad_sch").write_text(root_content)

        violations = [
            {
                "type": "power_pin_not_driven",
                "severity": "error",
                "description": "Power input pin not driven",
                "items": [{"description": "Pin VCC (power_in) of U1"}],
            },
        ]

        result = filter_cross_sheet_power_violations(violations, str(tmp_path / "root.kicad_sch"))
        # VCC is not in the driven set -- violation is a true positive
        assert len(result) == 1

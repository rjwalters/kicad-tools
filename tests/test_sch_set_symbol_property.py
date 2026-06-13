"""Tests for the sch set-symbol-property command.

Covers setting on_board, in_bom, dnp, and exclude_from_sim flags,
value normalization, dry-run, backup, invalid property rejection,
hierarchical traversal, and edge cases.
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.cli.modify_schematic import set_symbol_flag_text
from kicad_tools.cli.sch_set_symbol_property import (
    RECOGNIZED_FLAGS,
    _normalize_flag_value,
    run_set_symbol_property,
)

# ---------------------------------------------------------------------------
# Minimal schematic content for testing
# ---------------------------------------------------------------------------

MINIMAL_SCHEMATIC = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
  )

  (symbol
    (lib_id "power:GNDA")
    (at 100 50 0)
    (unit 1)
    (exclude_from_sim no)
    (in_bom yes)
    (on_board no)
    (dnp no)
    (uuid "11111111-1111-1111-1111-111111111111")
    (property "Reference" "#PWR052"
      (at 100 56 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property "Value" "GNDA"
      (at 100 53 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Footprint" ""
      (at 100 50 0)
      (effects (font (size 1.27 1.27)) hide)
    )
  )

  (symbol
    (lib_id "Device:R")
    (at 120 50 0)
    (unit 1)
    (exclude_from_sim no)
    (in_bom yes)
    (on_board yes)
    (dnp no)
    (uuid "22222222-2222-2222-2222-222222222222")
    (property "Reference" "R1"
      (at 120 46 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Value" "10k"
      (at 120 48 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Footprint" "Resistor_SMD:R_0402"
      (at 120 50 0)
      (effects (font (size 1.27 1.27)) hide)
    )
  )

  (sheet_instances
    (path "/" (page "1"))
  )
)
"""

# Schematic without exclude_from_sim (older format)
MINIMAL_SCHEMATIC_NO_EXCLUDE = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
  )

  (symbol
    (lib_id "Device:C")
    (at 140 50 0)
    (unit 1)
    (in_bom yes)
    (on_board yes)
    (dnp no)
    (uuid "33333333-3333-3333-3333-333333333333")
    (property "Reference" "C1"
      (at 140 46 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Value" "100nF"
      (at 140 48 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Footprint" "Capacitor_SMD:C_0402"
      (at 140 50 0)
      (effects (font (size 1.27 1.27)) hide)
    )
  )

  (sheet_instances
    (path "/" (page "1"))
  )
)
"""


def _write_sch(
    tmp_path: Path, content: str = MINIMAL_SCHEMATIC, name: str = "test.kicad_sch"
) -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# Value normalization
# ---------------------------------------------------------------------------


class TestNormalizeFlagValue:
    def test_yes_no(self):
        assert _normalize_flag_value("yes") == "yes"
        assert _normalize_flag_value("no") == "no"

    def test_true_false(self):
        assert _normalize_flag_value("true") == "yes"
        assert _normalize_flag_value("false") == "no"

    def test_numeric(self):
        assert _normalize_flag_value("1") == "yes"
        assert _normalize_flag_value("0") == "no"

    def test_case_insensitive(self):
        assert _normalize_flag_value("YES") == "yes"
        assert _normalize_flag_value("True") == "yes"
        assert _normalize_flag_value("FALSE") == "no"

    def test_invalid(self):
        assert _normalize_flag_value("maybe") is None
        assert _normalize_flag_value("") is None
        assert _normalize_flag_value("2") is None


# ---------------------------------------------------------------------------
# set_symbol_flag_text unit tests
# ---------------------------------------------------------------------------


class TestSetSymbolFlagText:
    def test_change_on_board_no_to_yes(self):
        modified, success, msg = set_symbol_flag_text(
            MINIMAL_SCHEMATIC, "#PWR052", "on_board", "yes"
        )
        assert success
        assert "(on_board yes)" in modified
        assert "Changed #PWR052 on_board" in msg
        # Verify other symbol unchanged
        assert '(property "Reference" "R1"' in modified

    def test_change_on_board_yes_to_no(self):
        modified, success, msg = set_symbol_flag_text(MINIMAL_SCHEMATIC, "R1", "on_board", "no")
        assert success
        assert "Changed R1 on_board" in msg
        # The power symbol should still have on_board no (its original value)
        # R1's on_board should now be no

    def test_change_in_bom(self):
        modified, success, msg = set_symbol_flag_text(MINIMAL_SCHEMATIC, "#PWR052", "in_bom", "no")
        assert success
        assert "Changed #PWR052 in_bom" in msg

    def test_change_dnp(self):
        modified, success, msg = set_symbol_flag_text(MINIMAL_SCHEMATIC, "R1", "dnp", "yes")
        assert success
        assert "Changed R1 dnp" in msg

    def test_change_exclude_from_sim(self):
        modified, success, msg = set_symbol_flag_text(
            MINIMAL_SCHEMATIC, "R1", "exclude_from_sim", "yes"
        )
        assert success
        assert "Changed R1 exclude_from_sim" in msg

    def test_already_set_returns_success(self):
        modified, success, msg = set_symbol_flag_text(
            MINIMAL_SCHEMATIC, "#PWR052", "on_board", "no"
        )
        assert success
        assert "already set" in msg
        assert modified == MINIMAL_SCHEMATIC  # no change

    def test_symbol_not_found(self):
        modified, success, msg = set_symbol_flag_text(MINIMAL_SCHEMATIC, "U99", "on_board", "yes")
        assert not success
        assert "not found" in msg
        assert modified == MINIMAL_SCHEMATIC

    def test_flag_not_found(self):
        modified, success, msg = set_symbol_flag_text(
            MINIMAL_SCHEMATIC_NO_EXCLUDE, "C1", "exclude_from_sim", "yes"
        )
        assert not success
        assert "not found" in msg.lower()

    def test_preserves_formatting(self):
        """Verify only the target flag value changes, not other content."""
        modified, success, msg = set_symbol_flag_text(
            MINIMAL_SCHEMATIC, "#PWR052", "on_board", "yes"
        )
        assert success
        # All other flags and properties should be unchanged
        assert "(in_bom yes)" in modified
        assert "(dnp no)" in modified
        assert "(exclude_from_sim no)" in modified
        assert '(property "Value" "GNDA"' in modified

    def test_only_target_symbol_changed(self):
        """Verify that changing on_board on #PWR052 doesn't affect R1."""
        modified, success, _ = set_symbol_flag_text(MINIMAL_SCHEMATIC, "#PWR052", "on_board", "yes")
        assert success
        # R1's symbol block should still have on_board yes
        # Find R1's uuid (unique to R1's block) and search backwards for on_board
        r1_uuid_idx = modified.index("22222222-2222-2222-2222-222222222222")
        r1_block_start = modified.rfind("(symbol", 0, r1_uuid_idx)
        r1_block = modified[r1_block_start : r1_uuid_idx + 50]
        assert "(on_board yes)" in r1_block


# ---------------------------------------------------------------------------
# run_set_symbol_property integration tests
# ---------------------------------------------------------------------------


class TestRunSetSymbolProperty:
    def test_basic_set(self, tmp_path):
        sch = _write_sch(tmp_path)
        rc = run_set_symbol_property(
            schematic_path=sch,
            ref="#PWR052",
            property_name="on_board",
            value="yes",
            dry_run=False,
            backup=False,
        )
        assert rc == 0
        content = sch.read_text()
        # The symbol block for #PWR052 should now have on_board yes
        pwr_idx = content.index("#PWR052")
        block_start = content.rfind("(symbol", 0, pwr_idx)
        block_snippet = content[block_start : pwr_idx + 100]
        assert "(on_board yes)" in block_snippet

    def test_dry_run_no_modification(self, tmp_path):
        sch = _write_sch(tmp_path)
        original = sch.read_text()
        rc = run_set_symbol_property(
            schematic_path=sch,
            ref="#PWR052",
            property_name="on_board",
            value="yes",
            dry_run=True,
            backup=False,
        )
        assert rc == 0
        assert sch.read_text() == original

    def test_backup_created(self, tmp_path):
        sch = _write_sch(tmp_path)
        rc = run_set_symbol_property(
            schematic_path=sch,
            ref="#PWR052",
            property_name="on_board",
            value="yes",
            dry_run=False,
            backup=True,
        )
        assert rc == 0
        backups = list(tmp_path.glob("*_backup_*"))
        assert len(backups) == 1

    def test_invalid_property_name(self, tmp_path):
        sch = _write_sch(tmp_path)
        rc = run_set_symbol_property(
            schematic_path=sch,
            ref="#PWR052",
            property_name="bogus",
            value="yes",
            dry_run=False,
            backup=False,
        )
        assert rc == 1

    def test_invalid_value(self, tmp_path):
        sch = _write_sch(tmp_path)
        rc = run_set_symbol_property(
            schematic_path=sch,
            ref="#PWR052",
            property_name="on_board",
            value="maybe",
            dry_run=False,
            backup=False,
        )
        assert rc == 1

    def test_symbol_not_found_error(self, tmp_path):
        sch = _write_sch(tmp_path)
        rc = run_set_symbol_property(
            schematic_path=sch,
            ref="U99",
            property_name="on_board",
            value="yes",
            dry_run=False,
            backup=False,
        )
        assert rc == 1

    def test_file_not_found_error(self, tmp_path):
        rc = run_set_symbol_property(
            schematic_path=tmp_path / "nonexistent.kicad_sch",
            ref="#PWR052",
            property_name="on_board",
            value="yes",
        )
        assert rc == 1

    def test_value_normalization_true(self, tmp_path):
        sch = _write_sch(tmp_path)
        rc = run_set_symbol_property(
            schematic_path=sch,
            ref="#PWR052",
            property_name="on_board",
            value="true",
            dry_run=False,
            backup=False,
        )
        assert rc == 0
        content = sch.read_text()
        pwr_idx = content.index("#PWR052")
        block_start = content.rfind("(symbol", 0, pwr_idx)
        block_snippet = content[block_start : pwr_idx + 100]
        assert "(on_board yes)" in block_snippet

    def test_value_normalization_numeric(self, tmp_path):
        sch = _write_sch(tmp_path)
        rc = run_set_symbol_property(
            schematic_path=sch,
            ref="#PWR052",
            property_name="on_board",
            value="1",
            dry_run=False,
            backup=False,
        )
        assert rc == 0
        content = sch.read_text()
        pwr_idx = content.index("#PWR052")
        block_start = content.rfind("(symbol", 0, pwr_idx)
        block_snippet = content[block_start : pwr_idx + 100]
        assert "(on_board yes)" in block_snippet

    def test_other_symbol_unaffected(self, tmp_path):
        sch = _write_sch(tmp_path)
        rc = run_set_symbol_property(
            schematic_path=sch,
            ref="#PWR052",
            property_name="on_board",
            value="yes",
            dry_run=False,
            backup=False,
        )
        assert rc == 0
        content = sch.read_text()
        # R1's symbol block should still have on_board yes
        r1_uuid_idx = content.index("22222222-2222-2222-2222-222222222222")
        r1_block_start = content.rfind("(symbol", 0, r1_uuid_idx)
        r1_block = content[r1_block_start : r1_uuid_idx + 50]
        assert "(on_board yes)" in r1_block

    def test_missing_flag_returns_error(self, tmp_path):
        """Symbol without the target flag should return error."""
        sch = _write_sch(tmp_path, MINIMAL_SCHEMATIC_NO_EXCLUDE)
        rc = run_set_symbol_property(
            schematic_path=sch,
            ref="C1",
            property_name="exclude_from_sim",
            value="yes",
            dry_run=False,
            backup=False,
        )
        assert rc == 1


# ---------------------------------------------------------------------------
# Hierarchical traversal tests
# ---------------------------------------------------------------------------


class TestHierarchicalTraversal:
    def _create_hierarchy(self, tmp_path: Path) -> Path:
        """Create a root schematic that references a sub-sheet."""
        subsheet_content = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "44444444-4444-4444-4444-444444444444")
  (paper "A4")
  (lib_symbols
  )

  (symbol
    (lib_id "power:VCC")
    (at 50 50 0)
    (unit 1)
    (exclude_from_sim no)
    (in_bom no)
    (on_board no)
    (dnp no)
    (uuid "55555555-5555-5555-5555-555555555555")
    (property "Reference" "#PWR099"
      (at 50 46 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property "Value" "VCC"
      (at 50 48 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Footprint" ""
      (at 50 50 0)
      (effects (font (size 1.27 1.27)) hide)
    )
  )

  (sheet_instances
    (path "/sub/" (page "2"))
  )
)
"""
        sub_path = tmp_path / "subsheet.kicad_sch"
        sub_path.write_text(subsheet_content)

        root_content = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
  )

  (sheet
    (at 200 100) (size 20 15)
    (property "Sheetname" "Power"
      (at 200 99 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Sheetfile" "subsheet.kicad_sch"
      (at 200 115 0)
      (effects (font (size 1.27 1.27)))
    )
    (uuid "66666666-6666-6666-6666-666666666666")
  )

  (sheet_instances
    (path "/" (page "1"))
  )
)
"""
        root_path = tmp_path / "root.kicad_sch"
        root_path.write_text(root_content)
        return root_path

    def test_finds_symbol_in_subsheet(self, tmp_path):
        root = self._create_hierarchy(tmp_path)
        rc = run_set_symbol_property(
            schematic_path=root,
            ref="#PWR099",
            property_name="on_board",
            value="yes",
            dry_run=False,
            backup=False,
        )
        assert rc == 0
        sub = tmp_path / "subsheet.kicad_sch"
        content = sub.read_text()
        pwr_idx = content.index("#PWR099")
        block_start = content.rfind("(symbol", 0, pwr_idx)
        block_snippet = content[block_start : pwr_idx + 100]
        assert "(on_board yes)" in block_snippet

    def test_dry_run_subsheet(self, tmp_path):
        root = self._create_hierarchy(tmp_path)
        sub = tmp_path / "subsheet.kicad_sch"
        original = sub.read_text()
        rc = run_set_symbol_property(
            schematic_path=root,
            ref="#PWR099",
            property_name="on_board",
            value="yes",
            dry_run=True,
            backup=False,
        )
        assert rc == 0
        assert sub.read_text() == original


# ---------------------------------------------------------------------------
# Recognized flags constant
# ---------------------------------------------------------------------------


class TestRecognizedFlags:
    def test_contains_all_four(self):
        assert {"on_board", "in_bom", "dnp", "exclude_from_sim"} == RECOGNIZED_FLAGS

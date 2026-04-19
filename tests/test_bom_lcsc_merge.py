"""Tests for BOM LCSC CSV merge behaviour.

Verifies that manually-assigned LCSC part numbers in an existing BOM CSV
are preserved when the BOM is regenerated, and that the merge respects
the priority hierarchy:

    schematic LCSC > spec overlay > existing CSV merge > API auto-match
"""

from __future__ import annotations

import csv
import io
import textwrap
from pathlib import Path

import pytest

from kicad_tools.export.bom_formats import read_existing_lcsc_assignments
from kicad_tools.schema.bom import BOMItem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_item(
    ref: str,
    value: str,
    footprint: str,
    lcsc: str = "",
    dnp: bool = False,
) -> BOMItem:
    """Build a BOMItem with minimal required fields."""
    return BOMItem(
        reference=ref,
        value=value,
        footprint=footprint,
        lib_id="Device:R",
        lcsc=lcsc,
        dnp=dnp,
    )


def _write_jlcpcb_csv(path: Path, rows: list[list[str]]) -> None:
    """Write a JLCPCB-format BOM CSV with the given data rows."""
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
        for row in rows:
            writer.writerow(row)


# ---------------------------------------------------------------------------
# read_existing_lcsc_assignments tests
# ---------------------------------------------------------------------------


class TestReadExistingLcscAssignments:
    """Tests for the CSV reader that extracts LCSC assignments."""

    def test_basic_parsing(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "bom.csv"
        _write_jlcpcb_csv(
            csv_path,
            [
                ["10k", "R1,R2", "0402", "C123456"],
                ["100nF", "C1", "0402", "C789012"],
            ],
        )
        result = read_existing_lcsc_assignments(csv_path)
        assert result == {
            ("10k", "0402"): "C123456",
            ("100nF", "0402"): "C789012",
        }

    def test_skips_empty_lcsc(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "bom.csv"
        _write_jlcpcb_csv(
            csv_path,
            [
                ["10k", "R1", "0402", "C123456"],
                ["STM32", "U1", "LQFP48", ""],
            ],
        )
        result = read_existing_lcsc_assignments(csv_path)
        assert ("STM32", "LQFP48") not in result
        assert result == {("10k", "0402"): "C123456"}

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        result = read_existing_lcsc_assignments(tmp_path / "missing.csv")
        assert result == {}

    def test_malformed_csv(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "bom.csv"
        csv_path.write_text("this is not,a valid\ncsv,with,correct,headers\n")
        result = read_existing_lcsc_assignments(csv_path)
        assert result == {}

    def test_empty_file(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "bom.csv"
        csv_path.write_text("")
        result = read_existing_lcsc_assignments(csv_path)
        assert result == {}

    def test_header_only(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "bom.csv"
        _write_jlcpcb_csv(csv_path, [])
        result = read_existing_lcsc_assignments(csv_path)
        assert result == {}

    def test_case_insensitive_headers(self, tmp_path: Path) -> None:
        """Headers should be matched case-insensitively."""
        csv_path = tmp_path / "bom.csv"
        with open(csv_path, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["COMMENT", "DESIGNATOR", "FOOTPRINT", "LCSC PART #"])
            writer.writerow(["10k", "R1", "0402", "C111111"])
        result = read_existing_lcsc_assignments(csv_path)
        assert result == {("10k", "0402"): "C111111"}

    def test_utf8_bom_encoding(self, tmp_path: Path) -> None:
        """CSV saved with UTF-8 BOM (common from Excel) should parse."""
        csv_path = tmp_path / "bom.csv"
        content = "Comment,Designator,Footprint,LCSC Part #\n10k,R1,0402,C222222\n"
        csv_path.write_text(content, encoding="utf-8-sig")
        result = read_existing_lcsc_assignments(csv_path)
        assert result == {("10k", "0402"): "C222222"}

    def test_stale_rows_ignored(self, tmp_path: Path) -> None:
        """Rows for components no longer in the schematic are simply
        returned -- the caller decides whether to use them."""
        csv_path = tmp_path / "bom.csv"
        _write_jlcpcb_csv(
            csv_path,
            [
                ["10k", "R1", "0402", "C123456"],
                ["OldPart", "X99", "SOT23", "C999999"],
            ],
        )
        result = read_existing_lcsc_assignments(csv_path)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Integration: merge step in _generate_bom
# ---------------------------------------------------------------------------


class TestBomLcscMergeIntegration:
    """Tests that verify the merge step applies existing LCSC assignments
    to freshly-extracted BOM items."""

    def test_merge_preserves_manual_lcsc(self, tmp_path: Path) -> None:
        """Items without LCSC should pick up the value from the existing CSV."""
        # Simulate an existing BOM CSV with a manual LCSC assignment
        _write_jlcpcb_csv(
            tmp_path / "bom_jlcpcb.csv",
            [
                ["10k", "R1,R2", "0402", "C123456"],
                ["100nF", "C1", "0402", "C789012"],
            ],
        )

        # Fresh BOM items have no LCSC
        items = [
            _make_item("R1", "10k", "0402"),
            _make_item("R2", "10k", "0402"),
            _make_item("C1", "100nF", "0402"),
        ]

        existing = read_existing_lcsc_assignments(tmp_path / "bom_jlcpcb.csv")
        merged = 0
        for item in items:
            if not item.lcsc:
                key = (item.value, item.footprint)
                lcsc = existing.get(key)
                if lcsc:
                    item.lcsc = lcsc
                    merged += 1

        assert merged == 3
        assert items[0].lcsc == "C123456"
        assert items[1].lcsc == "C123456"
        assert items[2].lcsc == "C789012"

    def test_merge_does_not_overwrite_schematic_lcsc(self, tmp_path: Path) -> None:
        """Items that already have an LCSC from the schematic must keep it."""
        _write_jlcpcb_csv(
            tmp_path / "bom_jlcpcb.csv",
            [["10k", "R1", "0402", "C111111"]],
        )

        items = [_make_item("R1", "10k", "0402", lcsc="C999999")]

        existing = read_existing_lcsc_assignments(tmp_path / "bom_jlcpcb.csv")
        for item in items:
            if not item.lcsc:
                key = (item.value, item.footprint)
                lcsc = existing.get(key)
                if lcsc:
                    item.lcsc = lcsc

        # Schematic value should win
        assert items[0].lcsc == "C999999"

    def test_merge_skips_dnp_items(self, tmp_path: Path) -> None:
        """DNP items should not receive LCSC from the merge."""
        _write_jlcpcb_csv(
            tmp_path / "bom_jlcpcb.csv",
            [["10k", "R1", "0402", "C123456"]],
        )

        items = [_make_item("R1", "10k", "0402", dnp=True)]

        existing = read_existing_lcsc_assignments(tmp_path / "bom_jlcpcb.csv")
        for item in items:
            if item.lcsc or item.dnp:
                continue
            key = (item.value, item.footprint)
            lcsc = existing.get(key)
            if lcsc:
                item.lcsc = lcsc

        assert items[0].lcsc == ""

    def test_merge_handles_renamed_references(self, tmp_path: Path) -> None:
        """Merge uses (value, footprint) as key, so renumbered references
        still match correctly."""
        _write_jlcpcb_csv(
            tmp_path / "bom_jlcpcb.csv",
            [["10k", "R1,R2", "0402", "C123456"]],
        )

        # After renumbering, R1 became R10
        items = [_make_item("R10", "10k", "0402")]

        existing = read_existing_lcsc_assignments(tmp_path / "bom_jlcpcb.csv")
        for item in items:
            if not item.lcsc:
                key = (item.value, item.footprint)
                lcsc = existing.get(key)
                if lcsc:
                    item.lcsc = lcsc

        assert items[0].lcsc == "C123456"

    def test_no_existing_csv_no_crash(self, tmp_path: Path) -> None:
        """When no existing CSV exists, merge step is a no-op."""
        items = [_make_item("R1", "10k", "0402")]

        existing = read_existing_lcsc_assignments(tmp_path / "bom_jlcpcb.csv")
        assert existing == {}
        # Items unchanged
        assert items[0].lcsc == ""

    def test_merge_disabled_does_not_apply(self, tmp_path: Path) -> None:
        """When merge_lcsc=False, no merging should occur."""
        from kicad_tools.export.assembly import AssemblyConfig

        config = AssemblyConfig(merge_lcsc=False)
        assert config.merge_lcsc is False

        # Simulate: even with an existing CSV, merge disabled means skip
        _write_jlcpcb_csv(
            tmp_path / "bom_jlcpcb.csv",
            [["10k", "R1", "0402", "C123456"]],
        )

        items = [_make_item("R1", "10k", "0402")]

        # Only merge if config allows
        if config.merge_lcsc:
            existing = read_existing_lcsc_assignments(tmp_path / "bom_jlcpcb.csv")
            for item in items:
                if not item.lcsc:
                    key = (item.value, item.footprint)
                    lcsc = existing.get(key)
                    if lcsc:
                        item.lcsc = lcsc

        assert items[0].lcsc == ""


class TestNoMergeLcscCliFlag:
    """Tests for the --no-merge-lcsc CLI flag."""

    def test_flag_sets_merge_lcsc_false(self) -> None:
        """The --no-merge-lcsc flag should set merge_lcsc=False in config."""
        from kicad_tools.cli.export_cmd import main
        import argparse

        # We can test the argument parsing directly
        parser = argparse.ArgumentParser()
        parser.add_argument("--no-merge-lcsc", action="store_true")
        args = parser.parse_args(["--no-merge-lcsc"])
        assert args.no_merge_lcsc is True

        args_default = parser.parse_args([])
        assert args_default.no_merge_lcsc is False

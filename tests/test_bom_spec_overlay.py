"""Tests for BOM spec overlay (export.bom_spec_overlay)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.export.bom_spec_overlay import (
    SpecOverlayReport,
    apply_spec_overlay,
    expand_ref_range,
    find_spec_file,
)
from kicad_tools.schema.bom import BOMItem
from kicad_tools.spec.schema import BOMEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_item(
    ref: str,
    value: str = "10k",
    footprint: str = "R_0402",
    mpn: str = "",
    lcsc: str = "",
) -> BOMItem:
    return BOMItem(
        reference=ref,
        value=value,
        footprint=footprint,
        lib_id="Device:R",
        mpn=mpn,
        lcsc=lcsc,
    )


# ---------------------------------------------------------------------------
# expand_ref_range
# ---------------------------------------------------------------------------


class TestExpandRefRange:
    def test_single_ref(self):
        assert expand_ref_range("U1") == ["U1"]

    def test_range(self):
        assert expand_ref_range("Q1-Q4") == ["Q1", "Q2", "Q3", "Q4"]

    def test_range_with_higher_numbers(self):
        assert expand_ref_range("R10-R12") == ["R10", "R11", "R12"]

    def test_single_element_range(self):
        assert expand_ref_range("R5-R5") == ["R5"]

    def test_reversed_range(self):
        # Invalid range returns as-is
        assert expand_ref_range("R5-R3") == ["R5-R3"]

    def test_mixed_prefix_range(self):
        # Different prefixes -- not a valid range
        assert expand_ref_range("R1-C4") == ["R1-C4"]

    def test_non_numeric_ref(self):
        # No digits -- returned as-is
        assert expand_ref_range("TP_A-TP_B") == ["TP_A-TP_B"]

    def test_whitespace_stripped(self):
        assert expand_ref_range(" R1 - R3 ") == ["R1", "R2", "R3"]


# ---------------------------------------------------------------------------
# apply_spec_overlay
# ---------------------------------------------------------------------------


class TestApplySpecOverlay:
    def test_single_ref_mpn_and_lcsc(self):
        items = [_make_item("U1", value="STM32")]
        entries = [BOMEntry(ref="U1", part="STM32G031F6P6", lcsc="C529330")]

        report = apply_spec_overlay(items, entries)

        assert items[0].mpn == "STM32G031F6P6"
        assert items[0].lcsc == "C529330"
        assert report.matched == 1
        assert report.unmatched == 0

    def test_range_expansion(self):
        items = [
            _make_item("Q1"),
            _make_item("Q2"),
            _make_item("Q3"),
            _make_item("Q4"),
        ]
        entries = [BOMEntry(ref="Q1-Q4", part="2N7002", lcsc="C8545")]

        report = apply_spec_overlay(items, entries)

        for item in items:
            assert item.mpn == "2N7002"
            assert item.lcsc == "C8545"
        assert report.matched == 4

    def test_missing_ref_warns_not_errors(self):
        items = [_make_item("R1")]
        entries = [BOMEntry(ref="U99", part="MISSING_PART")]

        report = apply_spec_overlay(items, entries)

        # R1 unchanged
        assert items[0].mpn == ""
        assert report.unmatched == 1
        assert "U99" in report.unmatched_refs

    def test_no_entries(self):
        items = [_make_item("R1")]
        report = apply_spec_overlay(items, [])
        assert report.total == 0
        assert items[0].mpn == ""

    def test_lcsc_only_no_mpn_change(self):
        """When part is empty string, mpn should still be set (but to empty)."""
        items = [_make_item("C1", mpn="existing")]
        entries = [BOMEntry(ref="C1", part="", lcsc="C1525")]

        apply_spec_overlay(items, entries)
        # part="" is falsy so mpn is not overwritten
        assert items[0].mpn == "existing"
        assert items[0].lcsc == "C1525"

    def test_mpn_only_no_lcsc(self):
        items = [_make_item("U1")]
        entries = [BOMEntry(ref="U1", part="ATmega328P")]

        apply_spec_overlay(items, entries)
        assert items[0].mpn == "ATmega328P"
        assert items[0].lcsc == ""  # no LCSC set

    def test_multiple_entries(self):
        items = [_make_item("U1"), _make_item("R1"), _make_item("C1")]
        entries = [
            BOMEntry(ref="U1", part="STM32G031F6P6", lcsc="C529330"),
            BOMEntry(ref="C1", part="GRM155R71C104K", lcsc="C1525"),
        ]

        report = apply_spec_overlay(items, entries)

        assert items[0].mpn == "STM32G031F6P6"
        assert items[2].lcsc == "C1525"
        # R1 untouched
        assert items[1].mpn == ""
        assert report.matched == 2


# ---------------------------------------------------------------------------
# SpecOverlayReport
# ---------------------------------------------------------------------------


class TestSpecOverlayReport:
    def test_summary_all_matched(self):
        from kicad_tools.export.bom_spec_overlay import SpecOverlayEntry

        report = SpecOverlayReport(
            entries=[
                SpecOverlayEntry(reference="U1", mpn="X", lcsc="C123", matched=True),
            ]
        )
        lines = report.summary_lines()
        assert "1 applied" in lines[0]
        assert "0 unmatched" in lines[0]

    def test_summary_with_unmatched(self):
        from kicad_tools.export.bom_spec_overlay import SpecOverlayEntry

        report = SpecOverlayReport(
            entries=[
                SpecOverlayEntry(reference="U99", mpn="X", lcsc="", matched=False),
            ]
        )
        lines = report.summary_lines()
        assert "1 unmatched" in lines[0]
        assert "U99" in lines[1]


# ---------------------------------------------------------------------------
# find_spec_file
# ---------------------------------------------------------------------------


class TestFindSpecFile:
    def test_no_kct_files(self, tmp_path: Path):
        assert find_spec_file(tmp_path) is None

    def test_project_kct_preferred(self, tmp_path: Path):
        (tmp_path / "project.kct").write_text("kct_version: '1.0'")
        (tmp_path / "other.kct").write_text("kct_version: '1.0'")
        result = find_spec_file(tmp_path)
        assert result is not None
        assert result.name == "project.kct"

    def test_single_kct(self, tmp_path: Path):
        (tmp_path / "myboard.kct").write_text("kct_version: '1.0'")
        result = find_spec_file(tmp_path)
        assert result is not None
        assert result.name == "myboard.kct"

    def test_multiple_kct_warns(self, tmp_path: Path):
        (tmp_path / "a.kct").write_text("kct_version: '1.0'")
        (tmp_path / "b.kct").write_text("kct_version: '1.0'")
        with patch("kicad_tools.export.bom_spec_overlay.logger") as mock_logger:
            result = find_spec_file(tmp_path)
            assert result is not None
            mock_logger.warning.assert_called_once()

    def test_find_spec_file_in_parent_dir(self, tmp_path: Path):
        """A .kct in the parent directory is found when PCB is in a subdirectory."""
        (tmp_path / "project.kct").write_text("kct_version: '1.0'")
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        result = find_spec_file(output_dir)
        assert result is not None
        assert result.name == "project.kct"
        assert result.parent == tmp_path

    def test_find_spec_file_stops_at_git_boundary(self, tmp_path: Path):
        """Walk-up should not cross a .git boundary."""
        # Place .kct above a directory that contains .git
        (tmp_path / "project.kct").write_text("kct_version: '1.0'")
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()  # simulates a git repo root
        output_dir = repo_dir / "output"
        output_dir.mkdir()

        result = find_spec_file(output_dir)
        # Should stop at repo_dir (which has .git) and NOT find the .kct above
        assert result is None

    def test_find_spec_file_stops_at_root(self, tmp_path: Path):
        """No infinite loop when no .kct exists anywhere."""
        deep_dir = tmp_path / "a" / "b" / "c"
        deep_dir.mkdir(parents=True)

        result = find_spec_file(deep_dir)
        assert result is None

    def test_find_spec_file_prefers_same_dir(self, tmp_path: Path):
        """If .kct exists in both current and parent, use current."""
        (tmp_path / "project.kct").write_text("kct_version: '1.0'")
        child = tmp_path / "child"
        child.mkdir()
        (child / "project.kct").write_text("kct_version: '1.0'")

        result = find_spec_file(child)
        assert result is not None
        assert result.parent == child

    def test_find_spec_file_parent_logs_info(self, tmp_path: Path):
        """An INFO log is emitted when the spec is found in a parent directory."""
        (tmp_path / "project.kct").write_text("kct_version: '1.0'")
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        with patch("kicad_tools.export.bom_spec_overlay.logger") as mock_logger:
            result = find_spec_file(output_dir)
            assert result is not None
            mock_logger.info.assert_called_once()
            assert "parent" in mock_logger.info.call_args[0][0].lower()


# ---------------------------------------------------------------------------
# Integration: spec source in enrichment report
# ---------------------------------------------------------------------------


class TestSpecSourceInEnrichment:
    """Verify that enrich_bom_lcsc reports 'spec' source for spec-populated refs."""

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_spec_source_reported(self, MockSuggester):
        from kicad_tools.export.bom_enrich import enrich_bom_lcsc

        mock_instance = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        mock_instance.__enter__ = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(
            return_value=mock_instance
        )
        mock_instance.__exit__ = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(
            return_value=False
        )
        MockSuggester.return_value = mock_instance

        items = [_make_item("U1", lcsc="C529330")]
        report = enrich_bom_lcsc(items, spec_refs={"U1"})

        assert report.entries[0].source == "spec"
        assert report.spec_populated == 1

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_schematic_source_without_spec_refs(self, MockSuggester):
        from kicad_tools.export.bom_enrich import enrich_bom_lcsc

        mock_instance = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        mock_instance.__enter__ = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(
            return_value=mock_instance
        )
        mock_instance.__exit__ = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(
            return_value=False
        )
        MockSuggester.return_value = mock_instance

        items = [_make_item("U1", lcsc="C529330")]
        report = enrich_bom_lcsc(items)

        assert report.entries[0].source == "schematic"
        assert report.spec_populated == 0


# ---------------------------------------------------------------------------
# Schema: BOMEntry in ProjectSpec
# ---------------------------------------------------------------------------


class TestBOMEntrySchema:
    def test_bom_entries_parsed(self):
        from kicad_tools.spec.schema import ProjectMetadata, ProjectSpec

        spec = ProjectSpec(
            project=ProjectMetadata(name="test"),
            bom_entries=[
                BOMEntry(ref="U1", part="STM32G031F6P6", lcsc="C529330"),
                BOMEntry(ref="Q1-Q4", part="2N7002"),
            ],
        )
        assert spec.bom_entries is not None
        assert len(spec.bom_entries) == 2
        assert spec.bom_entries[0].lcsc == "C529330"
        assert spec.bom_entries[1].lcsc is None

    def test_bom_entries_optional(self):
        from kicad_tools.spec.schema import ProjectMetadata, ProjectSpec

        spec = ProjectSpec(project=ProjectMetadata(name="test"))
        assert spec.bom_entries is None

    def test_bom_entries_roundtrip_yaml(self, tmp_path: Path):
        """Verify bom_entries survive save/load cycle."""
        from kicad_tools.spec.parser import load_spec, save_spec
        from kicad_tools.spec.schema import ProjectMetadata, ProjectSpec

        spec = ProjectSpec(
            project=ProjectMetadata(name="test"),
            bom_entries=[
                BOMEntry(ref="U1", part="STM32G031F6P6", lcsc="C529330"),
            ],
        )
        path = tmp_path / "test.kct"
        save_spec(spec, path)
        loaded = load_spec(path)
        assert loaded.bom_entries is not None
        assert len(loaded.bom_entries) == 1
        assert loaded.bom_entries[0].ref == "U1"
        assert loaded.bom_entries[0].part == "STM32G031F6P6"
        assert loaded.bom_entries[0].lcsc == "C529330"

"""Tests for the unused library item detection (purge) feature."""

from __future__ import annotations

import json
from pathlib import Path

from kicad_tools.library.purge import PurgeResult, UnusedItem, UnusedLibraryAnalyzer

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PURGE_PROJECT = FIXTURES_DIR / "purge_test_project"


class TestUnusedLibraryAnalyzer:
    """Tests for UnusedLibraryAnalyzer."""

    def test_detects_unused_symbols(self) -> None:
        """Symbols not referenced by any schematic are reported as unused."""
        analyzer = UnusedLibraryAnalyzer(PURGE_PROJECT)
        result = analyzer.analyze()

        unused_sym_ids = {item.lib_id for item in result.unused_symbols}
        assert "my_project_lib:UnusedSymbol" in unused_sym_ids
        assert "my_project_lib:AnotherUnused" in unused_sym_ids

    def test_used_symbols_not_reported(self) -> None:
        """Symbols referenced in schematics are NOT reported as unused."""
        analyzer = UnusedLibraryAnalyzer(PURGE_PROJECT)
        result = analyzer.analyze()

        unused_sym_ids = {item.lib_id for item in result.unused_symbols}
        assert "my_project_lib:UsedSymbol" not in unused_sym_ids

    def test_detects_unused_footprints(self) -> None:
        """Footprints not referenced by any PCB or schematic are reported."""
        analyzer = UnusedLibraryAnalyzer(PURGE_PROJECT)
        result = analyzer.analyze()

        unused_fp_ids = {item.lib_id for item in result.unused_footprints}
        assert "my_project_lib:UnusedFootprint" in unused_fp_ids

    def test_used_footprints_not_reported(self) -> None:
        """Footprints referenced in PCB or schematic are NOT reported."""
        analyzer = UnusedLibraryAnalyzer(PURGE_PROJECT)
        result = analyzer.analyze()

        unused_fp_ids = {item.lib_id for item in result.unused_footprints}
        assert "my_project_lib:UsedFootprint" not in unused_fp_ids

    def test_total_unused_count(self) -> None:
        """total_unused property returns the combined count."""
        analyzer = UnusedLibraryAnalyzer(PURGE_PROJECT)
        result = analyzer.analyze()

        # 2 unused symbols + 1 unused footprint = 3
        assert result.total_unused == 3

    def test_empty_project_dir(self, tmp_path: Path) -> None:
        """Empty directory produces no unused items."""
        analyzer = UnusedLibraryAnalyzer(tmp_path)
        result = analyzer.analyze()

        assert result.total_unused == 0
        assert result.unused_symbols == []
        assert result.unused_footprints == []

    def test_all_items_used(self, tmp_path: Path) -> None:
        """When all library items are referenced, nothing is unused."""
        # Create a minimal project where the only symbol is used
        sym_file = tmp_path / "mylib.kicad_sym"
        sym_file.write_text(
            '(kicad_symbol_lib (version 20231120) (symbol "OnlySymbol"))',
            encoding="utf-8",
        )
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(
            '(kicad_sch (version 20231120) (symbol (lib_id "mylib:OnlySymbol")))',
            encoding="utf-8",
        )

        analyzer = UnusedLibraryAnalyzer(tmp_path)
        result = analyzer.analyze()

        assert len(result.unused_symbols) == 0

    def test_missing_files_handled_gracefully(self, tmp_path: Path) -> None:
        """Corrupt or unreadable files do not cause crashes."""
        bad_sym = tmp_path / "bad.kicad_sym"
        bad_sym.write_text("this is not valid s-expression data!!!", encoding="utf-8")

        analyzer = UnusedLibraryAnalyzer(tmp_path)
        result = analyzer.analyze()

        # Should not raise, just produce an empty result
        assert result.total_unused == 0

    def test_library_with_no_unused_footprints(self, tmp_path: Path) -> None:
        """A .pretty dir where all footprints are used yields zero unused."""
        pretty_dir = tmp_path / "mylib.pretty"
        pretty_dir.mkdir()
        (pretty_dir / "FP1.kicad_mod").write_text(
            '(footprint "FP1" (layer "F.Cu"))', encoding="utf-8"
        )

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(
            '(kicad_pcb (version 20231120) (footprint "mylib:FP1" (layer "F.Cu")))',
            encoding="utf-8",
        )

        analyzer = UnusedLibraryAnalyzer(tmp_path)
        result = analyzer.analyze()

        assert len(result.unused_footprints) == 0


class TestPurgeResult:
    """Tests for PurgeResult formatting."""

    def _make_result(self) -> PurgeResult:
        return PurgeResult(
            project_dir=Path("/tmp/test"),
            unused_symbols=[
                UnusedItem(
                    library="mylib",
                    name="Sym1",
                    file_path=Path("/tmp/test/mylib.kicad_sym"),
                ),
            ],
            unused_footprints=[
                UnusedItem(
                    library="mylib",
                    name="FP1",
                    file_path=Path("/tmp/test/mylib.pretty/FP1.kicad_mod"),
                ),
            ],
        )

    def test_format_table(self) -> None:
        result = self._make_result()
        table = result.format_table()

        assert "Unused Symbols" in table
        assert "mylib:Sym1" in table
        assert "Unused Footprints" in table
        assert "mylib:FP1" in table

    def test_format_json(self) -> None:
        result = self._make_result()
        raw = result.format_json()
        data = json.loads(raw)

        assert data["total_unused"] == 2
        assert len(data["unused_symbols"]) == 1
        assert data["unused_symbols"][0]["lib_id"] == "mylib:Sym1"
        assert len(data["unused_footprints"]) == 1
        assert data["unused_footprints"][0]["lib_id"] == "mylib:FP1"

    def test_format_table_empty(self) -> None:
        result = PurgeResult(project_dir=Path("/tmp/empty"))
        assert "No unused library items found" in result.format_table()

    def test_format_json_empty(self) -> None:
        result = PurgeResult(project_dir=Path("/tmp/empty"))
        data = json.loads(result.format_json())
        assert data["total_unused"] == 0
        assert data["unused_symbols"] == []
        assert data["unused_footprints"] == []


class TestUnusedItem:
    """Tests for UnusedItem dataclass."""

    def test_lib_id_property(self) -> None:
        item = UnusedItem(library="mylib", name="MySym", file_path=Path("/tmp/mylib.kicad_sym"))
        assert item.lib_id == "mylib:MySym"

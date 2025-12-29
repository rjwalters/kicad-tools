"""Tests for CLI command modules."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from io import StringIO

from kicad_tools.cli import bom_cmd, nets, symbols
from kicad_tools.schema.bom import BOMItem, BOM


class TestBomCmdHelpers:
    """Tests for bom_cmd helper functions."""

    def test_group_items_empty(self):
        """Group empty list returns empty."""
        result = bom_cmd.group_items([])
        assert result == []

    def test_group_items_single(self):
        """Group single item."""
        item = BOMItem(
            reference="R1",
            value="10k",
            footprint="0603",
            lib_id="Device:R",
            mpn="RC0603FR-07100KL",
        )
        result = bom_cmd.group_items([item])
        assert len(result) == 1
        assert result[0]["quantity"] == 1
        assert result[0]["references"] == ["R1"]

    def test_group_items_multiple_same(self):
        """Group identical items."""
        items = [
            BOMItem("R1", "10k", "0603", "Device:R", "a", mpn="MPN1"),
            BOMItem("R2", "10k", "0603", "Device:R", "b", mpn="MPN1"),
            BOMItem("R3", "10k", "0603", "Device:R", "c", mpn="MPN1"),
        ]
        result = bom_cmd.group_items(items)
        assert len(result) == 1
        assert result[0]["quantity"] == 3
        assert set(result[0]["references"]) == {"R1", "R2", "R3"}

    def test_group_items_different(self):
        """Group different items."""
        items = [
            BOMItem("R1", "10k", "0603", "Device:R", "a"),
            BOMItem("C1", "100nF", "0603", "Device:C", "b"),
        ]
        result = bom_cmd.group_items(items)
        assert len(result) == 2

    def test_output_table_empty(self, capsys):
        """Output empty BOM table."""
        bom_cmd.output_table([], grouped=False)
        captured = capsys.readouterr()
        assert "No components found" in captured.out

    def test_output_table_ungrouped(self, capsys):
        """Output ungrouped BOM table."""
        items = [
            BOMItem("R1", "10k", "0603", "Device:R", "a"),
            BOMItem("C1", "100nF", "0603", "Device:C", "b"),
        ]
        bom_cmd.output_table(items, grouped=False)
        captured = capsys.readouterr()
        assert "R1" in captured.out
        assert "10k" in captured.out
        assert "Total: 2 components" in captured.out

    def test_output_table_grouped(self, capsys):
        """Output grouped BOM table."""
        groups = [
            {"quantity": 3, "value": "10k", "footprint": "0603", "mpn": "", "references": ["R1", "R2", "R3"]},
        ]
        bom_cmd.output_table(groups, grouped=True)
        captured = capsys.readouterr()
        assert "3" in captured.out
        assert "10k" in captured.out
        assert "Total: 1 groups" in captured.out

    def test_output_csv_ungrouped(self, capsys):
        """Output CSV ungrouped."""
        items = [
            BOMItem("R1", "10k", "0603", "Device:R", "a", mpn="MPN1"),
        ]
        bom_cmd.output_csv(items, grouped=False)
        captured = capsys.readouterr()
        assert "Reference,Value,Footprint,MPN" in captured.out
        assert "R1,10k,0603,MPN1" in captured.out

    def test_output_csv_grouped(self, capsys):
        """Output CSV grouped."""
        groups = [
            {"quantity": 2, "value": "10k", "footprint": "0603", "mpn": "MPN1", "references": ["R1", "R2"]},
        ]
        bom_cmd.output_csv(groups, grouped=True)
        captured = capsys.readouterr()
        assert "Quantity,Value,Footprint,MPN,References" in captured.out
        assert "2,10k,0603,MPN1" in captured.out

    def test_output_json_ungrouped(self, capsys):
        """Output JSON ungrouped."""
        items = [
            BOMItem("R1", "10k", "0603", "Device:R", "a", dnp=False),
        ]
        bom_cmd.output_json(items, grouped=False)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["total"] == 1
        assert data["items"][0]["reference"] == "R1"

    def test_output_json_grouped(self, capsys):
        """Output JSON grouped."""
        groups = [
            {"quantity": 2, "value": "10k", "footprint": "0603", "mpn": "", "references": ["R1", "R2"]},
        ]
        bom_cmd.output_json(groups, grouped=True)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["total_groups"] == 1
        assert data["total_components"] == 2


class TestBomCmdMain:
    """Tests for bom_cmd main function."""

    def test_main_file_not_found(self, tmp_path, capsys):
        """Main handles missing file gracefully (returns empty BOM or error)."""
        result = bom_cmd.main([str(tmp_path / "nonexistent.kicad_sch")])
        # Either returns 1 (error) or 0 (empty BOM)
        assert result in (0, 1)

    def test_main_with_schematic(self, simple_rc_schematic, capsys):
        """Main runs on valid schematic."""
        result = bom_cmd.main([str(simple_rc_schematic)])
        assert result == 0
        captured = capsys.readouterr()
        assert "R1" in captured.out
        assert "C1" in captured.out

    def test_main_csv_format(self, simple_rc_schematic, capsys):
        """Main with CSV format."""
        result = bom_cmd.main([str(simple_rc_schematic), "--format", "csv"])
        assert result == 0
        captured = capsys.readouterr()
        assert "Reference,Value" in captured.out

    def test_main_json_format(self, simple_rc_schematic, capsys):
        """Main with JSON format."""
        result = bom_cmd.main([str(simple_rc_schematic), "--format", "json"])
        assert result == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "items" in data

    def test_main_grouped(self, simple_rc_schematic, capsys):
        """Main with grouping enabled."""
        result = bom_cmd.main([str(simple_rc_schematic), "--group"])
        assert result == 0

    def test_main_exclude_pattern(self, simple_rc_schematic, capsys):
        """Main with exclude pattern."""
        result = bom_cmd.main([str(simple_rc_schematic), "--exclude", "R*"])
        assert result == 0
        captured = capsys.readouterr()
        assert "R1" not in captured.out

    def test_main_sort_by_value(self, simple_rc_schematic, capsys):
        """Main with sort by value."""
        result = bom_cmd.main([str(simple_rc_schematic), "--sort", "value"])
        assert result == 0


class TestNetsHelpers:
    """Tests for nets module helper functions."""

    def test_output_stats_empty(self, capsys):
        """Output stats for empty nets."""
        nets.output_stats([])
        captured = capsys.readouterr()
        assert "No nets found" in captured.out

    def test_output_all_table_empty(self, capsys):
        """Output empty nets table."""
        nets.output_all_table([])
        captured = capsys.readouterr()
        assert "No nets found" in captured.out

    def test_output_all_json(self, capsys):
        """Output nets as JSON."""
        from kicad_tools.operations.net_ops import Net, NetConnection
        from kicad_tools.schema.wire import Wire

        net = Net(name="TEST_NET", has_label=True)
        net.wires.append(Wire(start=(0, 0), end=(10, 0), uuid="w1"))
        net.connections.append(NetConnection(point=(0, 0), type="pin", reference="R1", pin_number="1"))

        nets.output_all_json([net])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data) == 1
        assert data[0]["name"] == "TEST_NET"


class TestNetsMain:
    """Tests for nets main function."""

    def test_main_file_not_found(self, tmp_path, capsys):
        """Main returns error for missing file."""
        result = nets.main([str(tmp_path / "nonexistent.kicad_sch")])
        assert result == 1
        captured = capsys.readouterr()
        assert "Error" in captured.err

    def test_main_with_schematic(self, simple_rc_schematic, capsys):
        """Main runs on valid schematic."""
        result = nets.main([str(simple_rc_schematic)])
        assert result == 0

    def test_main_stats_mode(self, simple_rc_schematic, capsys):
        """Main with stats mode."""
        result = nets.main([str(simple_rc_schematic), "--stats"])
        assert result == 0
        captured = capsys.readouterr()
        assert "Net Statistics" in captured.out or "Total" in captured.out

    def test_main_json_format(self, simple_rc_schematic, capsys):
        """Main with JSON format."""
        result = nets.main([str(simple_rc_schematic), "--format", "json"])
        assert result == 0
        captured = capsys.readouterr()
        # Should be valid JSON
        json.loads(captured.out)

    def test_main_specific_net(self, simple_rc_schematic, capsys):
        """Main tracing specific net."""
        result = nets.main([str(simple_rc_schematic), "--net", "VIN"])
        assert result == 0
        captured = capsys.readouterr()
        assert "VIN" in captured.out

    def test_main_specific_net_not_found(self, simple_rc_schematic, capsys):
        """Main returns error for nonexistent net."""
        result = nets.main([str(simple_rc_schematic), "--net", "NONEXISTENT"])
        assert result == 1


class TestSymbolsHelpers:
    """Tests for symbols module helper functions."""

    def test_output_table_empty(self, capsys):
        """Output empty symbols table."""
        symbols.output_table([], verbose=False)
        captured = capsys.readouterr()
        assert "No symbols found" in captured.out

    def test_output_csv(self, capsys):
        """Output symbols as CSV."""
        from kicad_tools.schema.symbol import SymbolInstance, SymbolProperty

        sym = SymbolInstance(
            lib_id="Device:R",
            position=(100, 50),
            rotation=0,
            unit=1,
            uuid="test-uuid",
            properties={
                "Reference": SymbolProperty(name="Reference", value="R1"),
                "Value": SymbolProperty(name="Value", value="10k"),
                "Footprint": SymbolProperty(name="Footprint", value="0603"),
            },
        )
        symbols.output_csv([sym], verbose=False)
        captured = capsys.readouterr()
        assert "Reference,Value,Library ID,Footprint" in captured.out
        assert "R1,10k,Device:R,0603" in captured.out

    def test_output_csv_verbose(self, capsys):
        """Output verbose symbols as CSV."""
        from kicad_tools.schema.symbol import SymbolInstance, SymbolProperty

        sym = SymbolInstance(
            lib_id="Device:R",
            position=(100, 50),
            rotation=90,
            unit=1,
            uuid="test-uuid",
            properties={
                "Reference": SymbolProperty(name="Reference", value="R1"),
                "Value": SymbolProperty(name="Value", value="10k"),
                "Footprint": SymbolProperty(name="Footprint", value="0603"),
            },
        )
        symbols.output_csv([sym], verbose=True)
        captured = capsys.readouterr()
        assert "X,Y,Rotation" in captured.out


class TestSymbolsMain:
    """Tests for symbols main function."""

    def test_main_file_not_found(self, tmp_path, capsys):
        """Main returns error for missing file."""
        result = symbols.main([str(tmp_path / "nonexistent.kicad_sch")])
        assert result == 1
        captured = capsys.readouterr()
        assert "Error" in captured.err

    def test_main_with_schematic(self, simple_rc_schematic, capsys):
        """Main runs on valid schematic."""
        result = symbols.main([str(simple_rc_schematic)])
        assert result == 0
        captured = capsys.readouterr()
        assert "R1" in captured.out
        assert "C1" in captured.out

    def test_main_json_format(self, simple_rc_schematic, capsys):
        """Main with JSON format."""
        result = symbols.main([str(simple_rc_schematic), "--format", "json"])
        assert result == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data) >= 2

    def test_main_csv_format(self, simple_rc_schematic, capsys):
        """Main with CSV format."""
        result = symbols.main([str(simple_rc_schematic), "--format", "csv"])
        assert result == 0

    def test_main_filter_pattern(self, simple_rc_schematic, capsys):
        """Main with filter pattern."""
        result = symbols.main([str(simple_rc_schematic), "--filter", "R*"])
        assert result == 0
        captured = capsys.readouterr()
        assert "R1" in captured.out
        # C1 should not appear
        lines = captured.out.strip().split("\n")
        symbol_lines = [l for l in lines if l.startswith("R") or l.startswith("C")]
        assert all("R" in l for l in symbol_lines if not l.startswith("-"))

    def test_main_verbose(self, simple_rc_schematic, capsys):
        """Main with verbose output."""
        result = symbols.main([str(simple_rc_schematic), "-v"])
        assert result == 0
        captured = capsys.readouterr()
        assert "Position" in captured.out or "Footprint" in captured.out

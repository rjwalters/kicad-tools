"""Tests for PCB-related CLI commands."""

import json
from pathlib import Path

import pytest


class TestPcbQuery:
    """Tests for pcb_query.py CLI."""

    def test_file_not_found(self, capsys, monkeypatch, tmp_path):
        """Test handling of missing file."""
        from kicad_tools.cli.pcb_query import main

        missing_file = tmp_path / "definitely_missing" / "nonexistent.kicad_pcb"
        monkeypatch.setattr("sys.argv", ["pcb-query", str(missing_file), "summary"])
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Error" in captured.err or "not found" in captured.err.lower()

    def test_summary_command(self, minimal_pcb: Path, capsys, monkeypatch):
        """Test summary command."""
        from kicad_tools.cli.pcb_query import main

        monkeypatch.setattr("sys.argv", ["pcb-query", str(minimal_pcb), "summary"])
        main()

        captured = capsys.readouterr()
        assert "PCB:" in captured.out
        assert len(captured.out) > 0

    def test_summary_json(self, minimal_pcb: Path, capsys, monkeypatch):
        """Test summary command with JSON output."""
        from kicad_tools.cli.pcb_query import main

        monkeypatch.setattr(
            "sys.argv", ["pcb-query", str(minimal_pcb), "summary", "--format", "json"]
        )
        main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, dict)

    def test_footprints_command(self, minimal_pcb: Path, capsys, monkeypatch):
        """Test footprints command."""
        from kicad_tools.cli.pcb_query import main

        monkeypatch.setattr("sys.argv", ["pcb-query", str(minimal_pcb), "footprints"])
        main()

        captured = capsys.readouterr()
        # Should output footprint info or "no footprints"
        assert len(captured.out) > 0

    def test_footprints_json(self, minimal_pcb: Path, capsys, monkeypatch):
        """Test footprints command with JSON output."""
        from kicad_tools.cli.pcb_query import main

        monkeypatch.setattr(
            "sys.argv", ["pcb-query", str(minimal_pcb), "footprints", "--format", "json"]
        )
        main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)

    def test_footprints_filter(self, minimal_pcb: Path, capsys, monkeypatch):
        """Test footprints command with filter."""
        from kicad_tools.cli.pcb_query import main

        monkeypatch.setattr(
            "sys.argv", ["pcb-query", str(minimal_pcb), "footprints", "--filter", "R*"]
        )
        main()

        captured = capsys.readouterr()
        # Should work without error
        assert len(captured.out) > 0

    def test_footprints_sorted(self, minimal_pcb: Path, capsys, monkeypatch):
        """Test footprints command with sorting."""
        from kicad_tools.cli.pcb_query import main

        monkeypatch.setattr("sys.argv", ["pcb-query", str(minimal_pcb), "footprints", "--sorted"])
        main()

        captured = capsys.readouterr()
        assert len(captured.out) > 0

    def test_footprint_detail(self, minimal_pcb: Path, capsys, monkeypatch):
        """Test footprint detail command."""
        from kicad_tools.cli.pcb_query import main

        # R1 exists in minimal_pcb fixture
        monkeypatch.setattr("sys.argv", ["pcb-query", str(minimal_pcb), "footprint", "R1"])
        main()

        captured = capsys.readouterr()
        assert "R1" in captured.out

    def test_nets_command(self, minimal_pcb: Path, capsys, monkeypatch):
        """Test nets command."""
        from kicad_tools.cli.pcb_query import main

        monkeypatch.setattr("sys.argv", ["pcb-query", str(minimal_pcb), "nets"])
        main()

        captured = capsys.readouterr()
        assert len(captured.out) > 0

    def test_nets_json(self, minimal_pcb: Path, capsys, monkeypatch):
        """Test nets command with JSON output."""
        from kicad_tools.cli.pcb_query import main

        monkeypatch.setattr("sys.argv", ["pcb-query", str(minimal_pcb), "nets", "--format", "json"])
        main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)

    def test_nets_sorted(self, minimal_pcb: Path, capsys, monkeypatch):
        """Test nets command with sorting."""
        from kicad_tools.cli.pcb_query import main

        monkeypatch.setattr("sys.argv", ["pcb-query", str(minimal_pcb), "nets", "--sorted"])
        main()

        captured = capsys.readouterr()
        assert len(captured.out) > 0

    def test_net_detail(self, minimal_pcb: Path, capsys, monkeypatch):
        """Test net detail command."""
        from kicad_tools.cli.pcb_query import main

        # GND exists in minimal_pcb fixture
        monkeypatch.setattr("sys.argv", ["pcb-query", str(minimal_pcb), "net", "GND"])
        main()

        captured = capsys.readouterr()
        assert "GND" in captured.out

    def test_traces_command(self, minimal_pcb: Path, capsys, monkeypatch):
        """Test traces command."""
        from kicad_tools.cli.pcb_query import main

        monkeypatch.setattr("sys.argv", ["pcb-query", str(minimal_pcb), "traces"])
        main()

        captured = capsys.readouterr()
        assert len(captured.out) > 0

    def test_traces_json(self, minimal_pcb: Path, capsys, monkeypatch):
        """Test traces command with JSON output."""
        from kicad_tools.cli.pcb_query import main

        monkeypatch.setattr(
            "sys.argv", ["pcb-query", str(minimal_pcb), "traces", "--format", "json"]
        )
        main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, dict)

    def test_vias_command(self, minimal_pcb: Path, capsys, monkeypatch):
        """Test vias command."""
        from kicad_tools.cli.pcb_query import main

        monkeypatch.setattr("sys.argv", ["pcb-query", str(minimal_pcb), "vias"])
        main()

        captured = capsys.readouterr()
        # May have "No vias" or via list
        assert len(captured.out) > 0

    def test_vias_json(self, minimal_pcb: Path, capsys, monkeypatch):
        """Test vias command with JSON output."""
        from kicad_tools.cli.pcb_query import main

        monkeypatch.setattr("sys.argv", ["pcb-query", str(minimal_pcb), "vias", "--format", "json"])
        main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)

    def test_stackup_command(self, minimal_pcb: Path, capsys, monkeypatch):
        """Test stackup command."""
        from kicad_tools.cli.pcb_query import main

        monkeypatch.setattr("sys.argv", ["pcb-query", str(minimal_pcb), "stackup"])
        main()

        captured = capsys.readouterr()
        # Should show layer stackup info
        assert len(captured.out) > 0

    def test_stackup_json(self, minimal_pcb: Path, capsys, monkeypatch):
        """Test stackup command with JSON output."""
        from kicad_tools.cli.pcb_query import main

        monkeypatch.setattr(
            "sys.argv", ["pcb-query", str(minimal_pcb), "stackup", "--format", "json"]
        )
        main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, (list, dict))

    def test_routing_test_pcb(self, routing_test_pcb: Path, capsys, monkeypatch):
        """Test with routing test PCB fixture."""
        from kicad_tools.cli.pcb_query import main

        monkeypatch.setattr("sys.argv", ["pcb-query", str(routing_test_pcb), "summary"])
        main()

        captured = capsys.readouterr()
        assert "PCB:" in captured.out

    def test_zone_test_pcb(self, zone_test_pcb: Path, capsys, monkeypatch):
        """Test with zone test PCB fixture."""
        from kicad_tools.cli.pcb_query import main

        monkeypatch.setattr("sys.argv", ["pcb-query", str(zone_test_pcb), "summary"])
        main()

        captured = capsys.readouterr()
        assert "PCB:" in captured.out


class TestPcbModify:
    """Tests for pcb_modify.py CLI.

    Note: Some tests require PCBs with 'fp_text reference' format (older KiCad style).
    The minimal_pcb fixture uses 'property "Reference"' format (KiCad 8 style),
    which is not supported by the current pcb_modify implementation.
    """

    @pytest.fixture
    def legacy_pcb(self, tmp_path):
        """Create a PCB with legacy fp_text format for modify tests."""
        pcb_content = """(kicad_pcb
  (version 20231120)
  (generator "test")
  (generator_version "7.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (at 100 100)
    (fp_text reference "R1" (at 0 -1.5) (layer "F.SilkS"))
    (fp_text value "10k" (at 0 1.5) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )
  (segment (start 100 100) (end 110 100) (width 0.2) (layer "F.Cu") (net 1))
)"""
        pcb_file = tmp_path / "legacy.kicad_pcb"
        pcb_file.write_text(pcb_content)
        return pcb_file

    def test_file_not_found(self, capsys, monkeypatch, tmp_path):
        """Test handling of missing file."""
        from kicad_tools.cli.pcb_modify import main

        missing_file = tmp_path / "definitely_missing" / "nonexistent.kicad_pcb"
        monkeypatch.setattr(
            "sys.argv", ["pcb-modify", str(missing_file), "move", "R1", "100", "100"]
        )
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Error" in captured.err or "not found" in captured.err.lower()

    def test_move_dry_run(self, legacy_pcb: Path, capsys, monkeypatch):
        """Test move command with dry run."""
        from kicad_tools.cli.pcb_modify import main

        monkeypatch.setattr(
            "sys.argv",
            ["pcb-modify", str(legacy_pcb), "move", "R1", "120", "130", "--dry-run"],
        )
        main()

        captured = capsys.readouterr()
        assert "Moving R1" in captured.out
        assert "120" in captured.out
        assert "130" in captured.out

    def test_move_to_output_file(self, legacy_pcb: Path, tmp_path, capsys, monkeypatch):
        """Test move command writing to output file."""
        from kicad_tools.cli.pcb_modify import main

        output_file = tmp_path / "output.kicad_pcb"
        monkeypatch.setattr(
            "sys.argv",
            [
                "pcb-modify",
                str(legacy_pcb),
                "move",
                "R1",
                "120",
                "130",
                "-o",
                str(output_file),
            ],
        )
        main()

        captured = capsys.readouterr()
        assert "Moving R1" in captured.out
        assert output_file.exists()

    def test_move_footprint_not_found(self, legacy_pcb: Path, capsys, monkeypatch):
        """Test move command with non-existent footprint."""
        from kicad_tools.cli.pcb_modify import main

        monkeypatch.setattr(
            "sys.argv",
            ["pcb-modify", str(legacy_pcb), "move", "NONEXISTENT", "100", "100", "--dry-run"],
        )
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "not found" in captured.err

    def test_rotate_dry_run(self, legacy_pcb: Path, capsys, monkeypatch):
        """Test rotate command with dry run."""
        from kicad_tools.cli.pcb_modify import main

        monkeypatch.setattr(
            "sys.argv",
            ["pcb-modify", str(legacy_pcb), "rotate", "R1", "90", "--dry-run"],
        )
        main()

        captured = capsys.readouterr()
        assert "Rotating R1" in captured.out
        assert "90" in captured.out

    def test_flip_dry_run(self, legacy_pcb: Path, capsys, monkeypatch):
        """Test flip command with dry run."""
        from kicad_tools.cli.pcb_modify import main

        monkeypatch.setattr("sys.argv", ["pcb-modify", str(legacy_pcb), "flip", "R1", "--dry-run"])
        main()

        captured = capsys.readouterr()
        assert "Flipping R1" in captured.out

    def test_update_value_dry_run(self, legacy_pcb: Path, capsys, monkeypatch):
        """Test update-value command with dry run."""
        from kicad_tools.cli.pcb_modify import main

        monkeypatch.setattr(
            "sys.argv",
            ["pcb-modify", str(legacy_pcb), "update-value", "R1", "22k", "--dry-run"],
        )
        main()

        captured = capsys.readouterr()
        assert "Updating value" in captured.out or "R1" in captured.out

    def test_rename_dry_run(self, legacy_pcb: Path, capsys, monkeypatch):
        """Test rename command with dry run."""
        from kicad_tools.cli.pcb_modify import main

        monkeypatch.setattr(
            "sys.argv",
            ["pcb-modify", str(legacy_pcb), "rename", "R1", "R10", "--dry-run"],
        )
        main()

        captured = capsys.readouterr()
        assert "Renaming" in captured.out or "R1" in captured.out

    def test_delete_traces_dry_run(self, legacy_pcb: Path, capsys, monkeypatch):
        """Test delete-traces command with dry run."""
        from kicad_tools.cli.pcb_modify import main

        monkeypatch.setattr(
            "sys.argv",
            ["pcb-modify", str(legacy_pcb), "delete-traces", "GND", "--dry-run"],
        )
        main()

        captured = capsys.readouterr()
        # Should show "Deleting" or "deleted" info
        assert "Deleting" in captured.out or "deleted" in captured.out.lower()

    def test_kicad8_pcb_footprint_not_found(self, minimal_pcb: Path, capsys, monkeypatch):
        """Test that KiCad 8 PCBs with 'property' format report footprint not found.

        This is a known limitation - the pcb_modify CLI expects 'fp_text reference'
        but KiCad 8 uses 'property "Reference"' format.
        """
        from kicad_tools.cli.pcb_modify import main

        monkeypatch.setattr(
            "sys.argv",
            ["pcb-modify", str(minimal_pcb), "move", "R1", "100", "100", "--dry-run"],
        )
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "not found" in captured.err

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

    def test_delete_traces_actually_removes_segments(self, tmp_path, capsys, monkeypatch):
        """Test delete-traces actually removes segments (not just dry-run).

        This is a regression test for issue #552 where delete-traces reported
        success but didn't actually remove segments due to deleting from a
        computed property (sexp.values) instead of the actual list (sexp.children).
        """
        from kicad_tools.cli.pcb_modify import main

        # Create a PCB with multiple segments on net GND
        pcb_content = """(kicad_pcb
  (version 20231120)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "VCC")
  (segment (start 0 0) (end 10 0) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 10 0) (end 20 0) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 20 0) (end 30 0) (width 0.2) (layer "F.Cu") (net 1))
  (via (at 15 0) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1))
  (segment (start 0 10) (end 10 10) (width 0.2) (layer "F.Cu") (net 2))
)"""
        pcb_file = tmp_path / "test_delete.kicad_pcb"
        pcb_file.write_text(pcb_content)

        # Run delete-traces on GND net (without dry-run)
        monkeypatch.setattr(
            "sys.argv",
            ["pcb-modify", str(pcb_file), "delete-traces", "GND"],
        )
        main()

        captured = capsys.readouterr()
        assert "Segments: 3" in captured.out
        assert "Vias:     1" in captured.out

        # Read the modified file and verify segments are gone
        modified_content = pcb_file.read_text()

        # GND segments should be removed
        assert "(net 1)" not in modified_content or modified_content.count("(net 1)") == 1
        # The net definition itself should still exist (net 1 "GND")
        assert '(net 1 "GND")' in modified_content
        # VCC segment should still exist
        assert "(net 2)" in modified_content

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


class TestPcbStrip:
    """Tests for 'kicad-tools pcb strip' command."""

    def test_strip_dry_run(self, minimal_pcb: Path, capsys):
        """Test strip command with dry run shows what would be removed."""
        from kicad_tools.cli.commands.pcb import run_pcb_command
        from argparse import Namespace

        args = Namespace(
            pcb=str(minimal_pcb),
            pcb_command="strip",
            output=None,
            nets=None,
            keep_zones=True,
            format="text",
            dry_run=True,
        )

        result = run_pcb_command(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "dry run" in captured.out.lower()
        assert "Removed:" in captured.out
        assert "Segments:" in captured.out

    def test_strip_creates_output_file(self, minimal_pcb: Path, tmp_path, capsys):
        """Test strip command creates output file."""
        from kicad_tools.cli.commands.pcb import run_pcb_command
        from argparse import Namespace

        output_file = tmp_path / "stripped.kicad_pcb"
        args = Namespace(
            pcb=str(minimal_pcb),
            pcb_command="strip",
            output=str(output_file),
            nets=None,
            keep_zones=True,
            format="text",
            dry_run=False,
        )

        result = run_pcb_command(args)

        assert result == 0
        assert output_file.exists()

        # Verify the output file has no traces
        from kicad_tools.schema.pcb import PCB

        stripped_pcb = PCB.load(output_file)
        assert len(stripped_pcb.segments) == 0

    def test_strip_json_output(self, minimal_pcb: Path, tmp_path, capsys):
        """Test strip command with JSON output format."""
        from kicad_tools.cli.commands.pcb import run_pcb_command
        from argparse import Namespace

        output_file = tmp_path / "stripped.kicad_pcb"
        args = Namespace(
            pcb=str(minimal_pcb),
            pcb_command="strip",
            output=str(output_file),
            nets=None,
            keep_zones=True,
            format="json",
            dry_run=False,
        )

        result = run_pcb_command(args)

        assert result == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "before" in data
        assert "removed" in data
        assert "after" in data
        assert "segments" in data["removed"]
        assert "vias" in data["removed"]

    def test_strip_specific_nets(self, tmp_path, capsys):
        """Test stripping only specific nets via CLI."""
        from kicad_tools.cli.commands.pcb import run_pcb_command
        from kicad_tools.schema.pcb import PCB
        from argparse import Namespace

        # Create a PCB with traces on multiple nets
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(
            start=(10.0, 10.0),
            end=(50.0, 10.0),
            width=0.25,
            layer="F.Cu",
            net="GND",
        )
        pcb.add_trace(
            start=(10.0, 20.0),
            end=(50.0, 20.0),
            width=0.25,
            layer="F.Cu",
            net="VCC",
        )

        input_file = tmp_path / "multi_net.kicad_pcb"
        pcb.save(input_file)

        output_file = tmp_path / "stripped.kicad_pcb"
        args = Namespace(
            pcb=str(input_file),
            pcb_command="strip",
            output=str(output_file),
            nets="GND",
            keep_zones=True,
            format="text",
            dry_run=False,
        )

        result = run_pcb_command(args)

        assert result == 0

        # Verify only GND trace was removed
        stripped_pcb = PCB.load(output_file)
        assert len(stripped_pcb.segments) == 1

    def test_strip_removes_zones_when_requested(self, zone_test_pcb: Path, tmp_path, capsys):
        """Test stripping zones with --no-keep-zones."""
        from kicad_tools.cli.commands.pcb import run_pcb_command
        from kicad_tools.schema.pcb import PCB
        from argparse import Namespace

        output_file = tmp_path / "stripped.kicad_pcb"
        args = Namespace(
            pcb=str(zone_test_pcb),
            pcb_command="strip",
            output=str(output_file),
            nets=None,
            keep_zones=False,
            format="text",
            dry_run=False,
        )

        result = run_pcb_command(args)

        assert result == 0

        # Verify zones were removed
        stripped_pcb = PCB.load(output_file)
        assert len(stripped_pcb.zones) == 0

    def test_strip_file_not_found(self, tmp_path, capsys):
        """Test strip command with non-existent file."""
        from kicad_tools.cli.commands.pcb import run_pcb_command
        from argparse import Namespace

        args = Namespace(
            pcb=str(tmp_path / "nonexistent.kicad_pcb"),
            pcb_command="strip",
            output=None,
            nets=None,
            keep_zones=True,
            format="text",
            dry_run=True,
        )

        result = run_pcb_command(args)

        assert result == 1
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "error" in captured.err.lower()

    def test_strip_default_output_suffix(self, minimal_pcb: Path, capsys):
        """Test that strip creates output with -stripped suffix when no output specified."""
        from kicad_tools.cli.commands.pcb import run_pcb_command
        from argparse import Namespace

        # minimal_pcb is already in tmp_path from the fixture
        args = Namespace(
            pcb=str(minimal_pcb),
            pcb_command="strip",
            output=None,
            nets=None,
            keep_zones=True,
            format="text",
            dry_run=False,
        )

        result = run_pcb_command(args)

        assert result == 0

        # Check that the stripped file was created with the suffix
        expected_output = minimal_pcb.with_stem(f"{minimal_pcb.stem}-stripped")
        assert expected_output.exists()


# PCB with multiple footprints for reannotation testing
MULTI_FOOTPRINT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
    (49 "F.Fab" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 100 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS")
      (effects (font (size 1.0 1.0) (thickness 0.15)))
      (uuid "00000000-0000-0000-0000-000000000101"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab")
      (uuid "00000000-0000-0000-0000-000000000102"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000002")
    (at 110 100)
    (property "Reference" "C2" (at 0 -1.5 0) (layer "F.SilkS")
      (effects (font (size 1.0 1.0) (thickness 0.15)))
      (uuid "00000000-0000-0000-0000-000000000201"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab")
      (uuid "00000000-0000-0000-0000-000000000202"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000003")
    (at 120 100)
    (property "Reference" "C3" (at 0 -1.5 0) (layer "F.SilkS")
      (effects (font (size 1.0 1.0) (thickness 0.15)))
      (uuid "00000000-0000-0000-0000-000000000301"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab")
      (uuid "00000000-0000-0000-0000-000000000302"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000004")
    (at 130 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS")
      (effects (font (size 1.0 1.0) (thickness 0.15)))
      (uuid "00000000-0000-0000-0000-000000000401"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab")
      (uuid "00000000-0000-0000-0000-000000000402"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000005")
    (at 140 100)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS")
      (effects (font (size 1.0 1.0) (thickness 0.15)))
      (uuid "00000000-0000-0000-0000-000000000501"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab")
      (uuid "00000000-0000-0000-0000-000000000502"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
)
"""


# KiCad 7 format PCB with fp_text instead of property
KICAD7_MULTI_FOOTPRINT_PCB = """(kicad_pcb
  (version 20230101)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
    (49 "F.Fab" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 100 100)
    (fp_text reference "R1" (at 0 -1.5 0) (layer "F.SilkS")
      (effects (font (size 1.0 1.0) (thickness 0.15))))
    (fp_text value "10k" (at 0 1.5 0) (layer "F.Fab")
      (effects (font (size 1.0 1.0) (thickness 0.15))))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000020")
    (at 110 100)
    (fp_text reference "R2" (at 0 -1.5 0) (layer "F.SilkS")
      (effects (font (size 1.0 1.0) (thickness 0.15))))
    (fp_text value "22k" (at 0 1.5 0) (layer "F.Fab")
      (effects (font (size 1.0 1.0) (thickness 0.15))))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
  )
)
"""


@pytest.fixture
def multi_fp_pcb(tmp_path: Path) -> Path:
    """Create a PCB with multiple footprints for reannotation testing."""
    pcb_file = tmp_path / "multi.kicad_pcb"
    pcb_file.write_text(MULTI_FOOTPRINT_PCB)
    return pcb_file


@pytest.fixture
def kicad7_multi_fp_pcb(tmp_path: Path) -> Path:
    """Create a KiCad 7 format PCB with multiple footprints."""
    pcb_file = tmp_path / "kicad7_multi.kicad_pcb"
    pcb_file.write_text(KICAD7_MULTI_FOOTPRINT_PCB)
    return pcb_file


class TestPcbReannotate:
    """Tests for pcb reannotate command."""

    def test_simple_rename(self, multi_fp_pcb: Path, tmp_path, capsys):
        """Test simple non-conflicting rename (A->B where B does not exist)."""
        from argparse import Namespace

        from kicad_tools.cli.commands.pcb import run_pcb_command
        from kicad_tools.schema.pcb import PCB

        # Create mapping file
        map_file = tmp_path / "map.json"
        map_file.write_text(json.dumps({"C1": "C10", "R1": "R10"}))

        output_file = tmp_path / "output.kicad_pcb"
        args = Namespace(
            pcb=str(multi_fp_pcb),
            pcb_command="reannotate",
            map=str(map_file),
            output=str(output_file),
            format="text",
            dry_run=False,
        )

        result = run_pcb_command(args)
        assert result == 0

        # Verify renames applied
        pcb = PCB.load(output_file)
        refs = {fp.reference for fp in pcb.footprints}
        assert "C10" in refs
        assert "R10" in refs
        assert "C1" not in refs
        assert "R1" not in refs
        # Unchanged refs still present
        assert "C2" in refs
        assert "C3" in refs
        assert "R2" in refs

    def test_collision_chain(self, multi_fp_pcb: Path, tmp_path, capsys):
        """Test collision chain: A->B, B->C where B is both source and target."""
        from argparse import Namespace

        from kicad_tools.cli.commands.pcb import run_pcb_command
        from kicad_tools.schema.pcb import PCB

        # C1->C2, C2->C3, C3->C10 (chain)
        map_file = tmp_path / "map.json"
        map_file.write_text(json.dumps({"C1": "C2", "C2": "C3", "C3": "C10"}))

        output_file = tmp_path / "output.kicad_pcb"
        args = Namespace(
            pcb=str(multi_fp_pcb),
            pcb_command="reannotate",
            map=str(map_file),
            output=str(output_file),
            format="text",
            dry_run=False,
        )

        result = run_pcb_command(args)
        assert result == 0

        # Verify: original C1 should now be C2, original C2 should be C3,
        # original C3 should be C10
        pcb = PCB.load(output_file)
        refs = {fp.reference for fp in pcb.footprints}
        assert "C2" in refs
        assert "C3" in refs
        assert "C10" in refs
        assert "C1" not in refs

    def test_cyclic_rename(self, multi_fp_pcb: Path, tmp_path, capsys):
        """Test cyclic rename: A->B, B->A (swap)."""
        from argparse import Namespace

        from kicad_tools.cli.commands.pcb import run_pcb_command
        from kicad_tools.schema.pcb import PCB

        map_file = tmp_path / "map.json"
        map_file.write_text(json.dumps({"C1": "C2", "C2": "C1"}))

        output_file = tmp_path / "output.kicad_pcb"
        args = Namespace(
            pcb=str(multi_fp_pcb),
            pcb_command="reannotate",
            map=str(map_file),
            output=str(output_file),
            format="text",
            dry_run=False,
        )

        result = run_pcb_command(args)
        assert result == 0

        # Load and check: both C1 and C2 should still exist (swapped)
        pcb = PCB.load(output_file)
        refs = {fp.reference for fp in pcb.footprints}
        assert "C1" in refs
        assert "C2" in refs

    def test_three_way_cycle(self, multi_fp_pcb: Path, tmp_path, capsys):
        """Test 3-way cyclic rename: A->B, B->C, C->A."""
        from argparse import Namespace

        from kicad_tools.cli.commands.pcb import run_pcb_command
        from kicad_tools.schema.pcb import PCB

        map_file = tmp_path / "map.json"
        map_file.write_text(json.dumps({"C1": "C2", "C2": "C3", "C3": "C1"}))

        output_file = tmp_path / "output.kicad_pcb"
        args = Namespace(
            pcb=str(multi_fp_pcb),
            pcb_command="reannotate",
            map=str(map_file),
            output=str(output_file),
            format="text",
            dry_run=False,
        )

        result = run_pcb_command(args)
        assert result == 0

        pcb = PCB.load(output_file)
        refs = {fp.reference for fp in pcb.footprints}
        # All three should still exist after the cycle
        assert "C1" in refs
        assert "C2" in refs
        assert "C3" in refs

    def test_dry_run_no_modification(self, multi_fp_pcb: Path, tmp_path, capsys):
        """Test --dry-run produces output without modifying the file."""
        from argparse import Namespace

        from kicad_tools.cli.commands.pcb import run_pcb_command

        map_file = tmp_path / "map.json"
        map_file.write_text(json.dumps({"C1": "C10"}))

        # Read original content
        original_content = multi_fp_pcb.read_text()

        args = Namespace(
            pcb=str(multi_fp_pcb),
            pcb_command="reannotate",
            map=str(map_file),
            output=None,
            format="text",
            dry_run=True,
        )

        result = run_pcb_command(args)
        assert result == 0

        # File should be unchanged
        assert multi_fp_pcb.read_text() == original_content

        captured = capsys.readouterr()
        assert "dry run" in captured.out.lower()
        assert "C1" in captured.out
        assert "C10" in captured.out

    def test_dry_run_json_format(self, multi_fp_pcb: Path, tmp_path, capsys):
        """Test --dry-run with JSON output format."""
        from argparse import Namespace

        from kicad_tools.cli.commands.pcb import run_pcb_command

        map_file = tmp_path / "map.json"
        map_file.write_text(json.dumps({"C1": "C10", "C2": "C20"}))

        args = Namespace(
            pcb=str(multi_fp_pcb),
            pcb_command="reannotate",
            map=str(map_file),
            output=None,
            format="json",
            dry_run=True,
        )

        result = run_pcb_command(args)
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["dry_run"] is True
        assert len(data["renames"]) > 0
        assert data["mapping"] == {"C1": "C10", "C2": "C20"}

    def test_json_output_format(self, multi_fp_pcb: Path, tmp_path, capsys):
        """Test JSON output format for actual renames."""
        from argparse import Namespace

        from kicad_tools.cli.commands.pcb import run_pcb_command

        map_file = tmp_path / "map.json"
        map_file.write_text(json.dumps({"C1": "C10"}))

        output_file = tmp_path / "output.kicad_pcb"
        args = Namespace(
            pcb=str(multi_fp_pcb),
            pcb_command="reannotate",
            map=str(map_file),
            output=str(output_file),
            format="json",
            dry_run=False,
        )

        result = run_pcb_command(args)
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["dry_run"] is False
        assert data["output"] == str(output_file)
        assert len(data["renames"]) == 1

    def test_missing_source_ref_error(self, multi_fp_pcb: Path, tmp_path, capsys):
        """Test error when source reference does not exist in PCB."""
        from argparse import Namespace

        from kicad_tools.cli.commands.pcb import run_pcb_command

        map_file = tmp_path / "map.json"
        map_file.write_text(json.dumps({"NONEXISTENT": "C10"}))

        args = Namespace(
            pcb=str(multi_fp_pcb),
            pcb_command="reannotate",
            map=str(map_file),
            output=None,
            format="text",
            dry_run=False,
        )

        result = run_pcb_command(args)
        assert result == 1

        captured = capsys.readouterr()
        assert "NONEXISTENT" in captured.err
        assert "not found" in captured.err.lower()

    def test_target_collides_with_existing_non_mapped_ref(
        self, multi_fp_pcb: Path, tmp_path, capsys
    ):
        """Test error when target collides with existing ref not in the mapping."""
        from argparse import Namespace

        from kicad_tools.cli.commands.pcb import run_pcb_command

        # C1 -> R1, but R1 exists and is NOT being renamed
        # Actually R1 is in the PCB. Map C1 to R2 (R2 also exists and is not mapped).
        map_file = tmp_path / "map.json"
        map_file.write_text(json.dumps({"C1": "R2"}))

        args = Namespace(
            pcb=str(multi_fp_pcb),
            pcb_command="reannotate",
            map=str(map_file),
            output=None,
            format="text",
            dry_run=False,
        )

        result = run_pcb_command(args)
        assert result == 1

        captured = capsys.readouterr()
        assert "R2" in captured.err
        assert "already exists" in captured.err.lower()

    def test_empty_mapping(self, multi_fp_pcb: Path, tmp_path, capsys):
        """Test empty mapping file is a no-op."""
        from argparse import Namespace

        from kicad_tools.cli.commands.pcb import run_pcb_command

        map_file = tmp_path / "map.json"
        map_file.write_text("{}")

        args = Namespace(
            pcb=str(multi_fp_pcb),
            pcb_command="reannotate",
            map=str(map_file),
            output=None,
            format="text",
            dry_run=False,
        )

        result = run_pcb_command(args)
        assert result == 0

    def test_empty_mapping_json_format(self, multi_fp_pcb: Path, tmp_path, capsys):
        """Test empty mapping with JSON output."""
        from argparse import Namespace

        from kicad_tools.cli.commands.pcb import run_pcb_command

        map_file = tmp_path / "map.json"
        map_file.write_text("{}")

        args = Namespace(
            pcb=str(multi_fp_pcb),
            pcb_command="reannotate",
            map=str(map_file),
            output=None,
            format="json",
            dry_run=False,
        )

        result = run_pcb_command(args)
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "no-op"

    def test_invalid_json_mapping(self, multi_fp_pcb: Path, tmp_path, capsys):
        """Test error on invalid JSON mapping file."""
        from argparse import Namespace

        from kicad_tools.cli.commands.pcb import run_pcb_command

        map_file = tmp_path / "map.json"
        map_file.write_text("not json at all")

        args = Namespace(
            pcb=str(multi_fp_pcb),
            pcb_command="reannotate",
            map=str(map_file),
            output=None,
            format="text",
            dry_run=False,
        )

        result = run_pcb_command(args)
        assert result == 1

        captured = capsys.readouterr()
        assert "invalid json" in captured.err.lower()

    def test_mapping_file_not_found(self, multi_fp_pcb: Path, tmp_path, capsys):
        """Test error when mapping file does not exist."""
        from argparse import Namespace

        from kicad_tools.cli.commands.pcb import run_pcb_command

        args = Namespace(
            pcb=str(multi_fp_pcb),
            pcb_command="reannotate",
            map=str(tmp_path / "nonexistent.json"),
            output=None,
            format="text",
            dry_run=False,
        )

        result = run_pcb_command(args)
        assert result == 1

        captured = capsys.readouterr()
        assert "not found" in captured.err.lower()

    def test_overwrite_input_when_no_output(self, multi_fp_pcb: Path, tmp_path, capsys):
        """Test that without -o, the input file is overwritten."""
        from argparse import Namespace

        from kicad_tools.cli.commands.pcb import run_pcb_command
        from kicad_tools.schema.pcb import PCB

        map_file = tmp_path / "map.json"
        map_file.write_text(json.dumps({"C1": "C10"}))

        args = Namespace(
            pcb=str(multi_fp_pcb),
            pcb_command="reannotate",
            map=str(map_file),
            output=None,
            format="text",
            dry_run=False,
        )

        result = run_pcb_command(args)
        assert result == 0

        # Verify input file was modified
        pcb = PCB.load(multi_fp_pcb)
        refs = {fp.reference for fp in pcb.footprints}
        assert "C10" in refs
        assert "C1" not in refs

    def test_kicad7_format_rename(self, kicad7_multi_fp_pcb: Path, tmp_path, capsys):
        """Test rename with KiCad 7 fp_text format."""
        from argparse import Namespace

        from kicad_tools.cli.commands.pcb import run_pcb_command
        from kicad_tools.schema.pcb import PCB

        map_file = tmp_path / "map.json"
        map_file.write_text(json.dumps({"R1": "R10", "R2": "R20"}))

        output_file = tmp_path / "output.kicad_pcb"
        args = Namespace(
            pcb=str(kicad7_multi_fp_pcb),
            pcb_command="reannotate",
            map=str(map_file),
            output=str(output_file),
            format="text",
            dry_run=False,
        )

        result = run_pcb_command(args)
        assert result == 0

        pcb = PCB.load(output_file)
        refs = {fp.reference for fp in pcb.footprints}
        assert "R10" in refs
        assert "R20" in refs
        assert "R1" not in refs
        assert "R2" not in refs

    def test_mapping_not_a_dict(self, multi_fp_pcb: Path, tmp_path, capsys):
        """Test error when mapping file contains a non-dict JSON value."""
        from argparse import Namespace

        from kicad_tools.cli.commands.pcb import run_pcb_command

        map_file = tmp_path / "map.json"
        map_file.write_text('["C1", "C2"]')

        args = Namespace(
            pcb=str(multi_fp_pcb),
            pcb_command="reannotate",
            map=str(map_file),
            output=None,
            format="text",
            dry_run=False,
        )

        result = run_pcb_command(args)
        assert result == 1

        captured = capsys.readouterr()
        assert "object" in captured.err.lower() or "dict" in captured.err.lower()

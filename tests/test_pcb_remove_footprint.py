"""Tests for pcb remove-footprint command (pcb_remove_footprint module)."""

import json

# Reuse PCB fixtures from sync-netlist tests
MINIMAL_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (uuid "fp-c1")
    (at 120 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "100n" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
)
"""

# PCB with a traced footprint
MINIMAL_PCB_WITH_TRACES = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
  (segment (start 99.5 100) (end 90 100) (width 0.25) (layer "F.Cu") (net 1))
)
"""


class TestRunRemoveFootprint:
    """Tests for run_remove_footprint function."""

    def test_removes_footprint(self, tmp_path):
        """Successfully removes an existing footprint."""
        from kicad_tools.cli.pcb_remove_footprint import run_remove_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        rc = run_remove_footprint(pcb, "C1")
        assert rc == 0

        board = PCB.load(pcb)
        assert board.get_footprint("C1") is None
        assert board.get_footprint("R1") is not None

    def test_dry_run_does_not_modify(self, tmp_path):
        """dry_run=True leaves file unchanged."""
        from kicad_tools.cli.pcb_remove_footprint import run_remove_footprint

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)
        original = pcb.read_text()

        rc = run_remove_footprint(pcb, "C1", dry_run=True)
        assert rc == 0
        assert pcb.read_text() == original

    def test_nonexistent_reference_returns_1(self, tmp_path):
        """Removing a non-existent reference returns error."""
        from kicad_tools.cli.pcb_remove_footprint import run_remove_footprint

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        rc = run_remove_footprint(pcb, "Z99")
        assert rc == 1

    def test_blocks_removal_with_traces(self, tmp_path):
        """Blocks removal when footprint has traces and force=False."""
        from kicad_tools.cli.pcb_remove_footprint import run_remove_footprint

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB_WITH_TRACES)

        rc = run_remove_footprint(pcb, "R1", force=False)
        assert rc == 1

    def test_force_removes_with_traces(self, tmp_path):
        """--force allows removal of footprint with traces."""
        from kicad_tools.cli.pcb_remove_footprint import run_remove_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB_WITH_TRACES)

        rc = run_remove_footprint(pcb, "R1", force=True)
        assert rc == 0

        board = PCB.load(pcb)
        assert board.get_footprint("R1") is None

    def test_output_path(self, tmp_path):
        """Writes to output path instead of overwriting input."""
        from kicad_tools.cli.pcb_remove_footprint import run_remove_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        out = tmp_path / "output.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        rc = run_remove_footprint(pcb, "C1", output_path=out)
        assert rc == 0

        # Original should be unchanged
        board_orig = PCB.load(pcb)
        assert board_orig.get_footprint("C1") is not None

        # Output should have C1 removed
        board_out = PCB.load(out)
        assert board_out.get_footprint("C1") is None

    def test_json_output(self, tmp_path, capsys):
        """JSON output contains expected fields."""
        from kicad_tools.cli.pcb_remove_footprint import run_remove_footprint

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        rc = run_remove_footprint(pcb, "C1", output_format="json")
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["reference"] == "C1"
        assert data["removed"] is True

    def test_text_output(self, tmp_path, capsys):
        """Text output contains key information."""
        from kicad_tools.cli.pcb_remove_footprint import run_remove_footprint

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        rc = run_remove_footprint(pcb, "C1", output_format="text")
        assert rc == 0

        captured = capsys.readouterr()
        assert "C1" in captured.out
        assert "Removed" in captured.out


class TestRemoveFootprintCLIParser:
    """Tests for the remove-footprint CLI parser."""

    def test_parser_has_remove_footprint_subcommand(self):
        """Parser supports 'pcb remove-footprint' subcommand."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "pcb",
                "remove-footprint",
                "--ref",
                "C1",
                "test.kicad_pcb",
            ]
        )
        assert args.pcb_command == "remove-footprint"
        assert args.ref == "C1"

    def test_parser_force_flag(self):
        """Parser accepts --force flag."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "pcb",
                "remove-footprint",
                "--ref",
                "C1",
                "--force",
                "test.kicad_pcb",
            ]
        )
        assert args.force is True

    def test_parser_dry_run_flag(self):
        """Parser accepts --dry-run flag."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "pcb",
                "remove-footprint",
                "--ref",
                "C1",
                "--dry-run",
                "test.kicad_pcb",
            ]
        )
        assert args.dry_run is True

    def test_dispatcher_integration(self, tmp_path):
        """Dispatcher correctly routes to remove-footprint handler."""
        from kicad_tools.cli.commands.pcb import _run_remove_footprint_command

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        class Args:
            ref = "C1"
            output = None
            dry_run = True
            force = False
            format = "text"

        rc = _run_remove_footprint_command(Args(), pcb)
        assert rc == 0

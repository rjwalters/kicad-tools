"""Tests for pcb move-footprint command (pcb_move_footprint module)."""

import json

import pytest

MINIMAL_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "GND")
  (footprint "Connector_JST:JST_XH_B2B"
    (layer "F.Cu")
    (uuid "fp-j2")
    (at 100 100)
    (property "Reference" "J2" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "Conn_01x02" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" thru_hole roundrect (at -1.25 0) (size 1.7 1.7) (layers "*.Cu") (net 1 "GND"))
    (pad "2" thru_hole roundrect (at 1.25 0) (size 1.7 1.7) (layers "*.Cu") (net 0 ""))
  )
  (footprint "Connector_JST:JST_XH_B3B"
    (layer "F.Cu")
    (uuid "fp-j3")
    (at 120 100 90)
    (property "Reference" "J3" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "Conn_01x03" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" thru_hole roundrect (at -2.5 0) (size 1.7 1.7) (layers "*.Cu") (net 1 "GND"))
    (pad "2" thru_hole roundrect (at 0 0) (size 1.7 1.7) (layers "*.Cu") (net 0 ""))
    (pad "3" thru_hole roundrect (at 2.5 0) (size 1.7 1.7) (layers "*.Cu") (net 0 ""))
  )
)
"""


class TestRunMoveFootprint:
    """Tests for run_move_footprint function."""

    def test_moves_footprint_position(self, tmp_path):
        """Successfully moves a footprint to new coordinates."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        rc = run_move_footprint(pcb, reference="J2", to=(132.5, 98.25))
        assert rc == 0

        board = PCB.load(pcb)
        fp = board.get_footprint("J2")
        assert fp is not None
        assert fp.position[0] == pytest.approx(132.5, abs=0.01)
        assert fp.position[1] == pytest.approx(98.25, abs=0.01)

    def test_moves_footprint_with_rotation(self, tmp_path):
        """Moves a footprint and sets new rotation."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        rc = run_move_footprint(pcb, reference="J2", to=(132.5, 98.25), rotation=90.0)
        assert rc == 0

        board = PCB.load(pcb)
        fp = board.get_footprint("J2")
        assert fp is not None
        assert fp.position[0] == pytest.approx(132.5, abs=0.01)
        assert fp.position[1] == pytest.approx(98.25, abs=0.01)
        assert fp.rotation == pytest.approx(90.0, abs=0.01)

    def test_batch_mode(self, tmp_path):
        """Batch mode moves multiple footprints."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        batch = {
            "J2": {"x": 132.5, "y": 98.25},
            "J3": {"x": 140.0, "y": 98.25},
        }
        rc = run_move_footprint(pcb, batch_map=batch)
        assert rc == 0

        board = PCB.load(pcb)
        j2 = board.get_footprint("J2")
        j3 = board.get_footprint("J3")
        assert j2 is not None
        assert j3 is not None
        assert j2.position[0] == pytest.approx(132.5, abs=0.01)
        assert j3.position[0] == pytest.approx(140.0, abs=0.01)

    def test_batch_mode_with_rotation(self, tmp_path):
        """Batch mode supports per-footprint rotation."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        batch = {
            "J2": {"x": 132.5, "y": 98.25, "rotation": 180.0},
        }
        rc = run_move_footprint(pcb, batch_map=batch)
        assert rc == 0

        board = PCB.load(pcb)
        fp = board.get_footprint("J2")
        assert fp is not None
        assert fp.rotation == pytest.approx(180.0, abs=0.01)

    def test_dry_run_does_not_modify(self, tmp_path):
        """dry_run=True leaves file unchanged."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)
        original = pcb.read_text()

        rc = run_move_footprint(pcb, reference="J2", to=(200.0, 200.0), dry_run=True)
        assert rc == 0
        assert pcb.read_text() == original

    def test_nonexistent_reference_returns_1(self, tmp_path):
        """Moving a non-existent reference returns error."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        rc = run_move_footprint(pcb, reference="Z99", to=(100.0, 100.0))
        assert rc == 1

    def test_batch_partial_failure_is_atomic(self, tmp_path):
        """If any reference in batch is invalid, no footprints are moved."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        batch = {
            "J2": {"x": 200.0, "y": 200.0},
            "Z99": {"x": 300.0, "y": 300.0},
        }
        rc = run_move_footprint(pcb, batch_map=batch)
        assert rc == 1

        # J2 should not have moved
        board = PCB.load(pcb)
        j2 = board.get_footprint("J2")
        assert j2 is not None
        assert j2.position[0] == pytest.approx(100.0, abs=0.01)

    def test_output_path(self, tmp_path):
        """Writes to output path instead of overwriting input."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        out = tmp_path / "output.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        rc = run_move_footprint(pcb, reference="J2", to=(150.0, 150.0), output_path=out)
        assert rc == 0

        # Original should be unchanged
        board_orig = PCB.load(pcb)
        j2_orig = board_orig.get_footprint("J2")
        assert j2_orig is not None
        assert j2_orig.position[0] == pytest.approx(100.0, abs=0.01)

        # Output should have J2 moved
        board_out = PCB.load(out)
        j2_out = board_out.get_footprint("J2")
        assert j2_out is not None
        assert j2_out.position[0] == pytest.approx(150.0, abs=0.01)

    def test_json_output(self, tmp_path, capsys):
        """JSON output contains expected fields."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        rc = run_move_footprint(pcb, reference="J2", to=(132.5, 98.25), output_format="json")
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["moved"] is True
        assert len(data["moves"]) == 1
        assert data["moves"][0]["reference"] == "J2"
        assert data["moves"][0]["new_position"] == [132.5, 98.25]
        assert data["moves"][0]["old_position"] == [100.0, 100.0]

    def test_text_output(self, tmp_path, capsys):
        """Text output contains key information."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        rc = run_move_footprint(pcb, reference="J2", to=(132.5, 98.25), output_format="text")
        assert rc == 0

        captured = capsys.readouterr()
        assert "J2" in captured.out
        assert "Moved" in captured.out

    def test_round_trip_integrity(self, tmp_path):
        """Load, move, save, reload -- verify only target footprint changed."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        # Record J3's original position
        board_before = PCB.load(pcb)
        j3_before = board_before.get_footprint("J3")
        assert j3_before is not None
        j3_pos_before = j3_before.position
        j3_rot_before = j3_before.rotation

        # Move only J2
        rc = run_move_footprint(pcb, reference="J2", to=(150.0, 75.0))
        assert rc == 0

        # Reload and verify
        board_after = PCB.load(pcb)
        j2 = board_after.get_footprint("J2")
        j3 = board_after.get_footprint("J3")
        assert j2 is not None
        assert j3 is not None
        assert j2.position[0] == pytest.approx(150.0, abs=0.01)
        assert j2.position[1] == pytest.approx(75.0, abs=0.01)
        # J3 should be unchanged
        assert j3.position[0] == pytest.approx(j3_pos_before[0], abs=0.01)
        assert j3.position[1] == pytest.approx(j3_pos_before[1], abs=0.01)
        assert j3.rotation == pytest.approx(j3_rot_before, abs=0.01)


class TestMoveFootprintCLIParser:
    """Tests for the move-footprint CLI parser."""

    def test_parser_has_move_footprint_subcommand(self):
        """Parser supports 'pcb move-footprint' subcommand."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "pcb",
                "move-footprint",
                "--ref",
                "J2",
                "--to",
                "132.5",
                "98.25",
                "test.kicad_pcb",
            ]
        )
        assert args.pcb_command == "move-footprint"
        assert args.ref == "J2"
        assert args.to == [132.5, 98.25]

    def test_parser_rotation_flag(self):
        """Parser accepts --rotation flag."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "pcb",
                "move-footprint",
                "--ref",
                "J2",
                "--to",
                "132.5",
                "98.25",
                "--rotation",
                "90",
                "test.kicad_pcb",
            ]
        )
        assert args.rotation == 90.0

    def test_parser_dry_run_flag(self):
        """Parser accepts --dry-run flag."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "pcb",
                "move-footprint",
                "--ref",
                "J2",
                "--to",
                "132.5",
                "98.25",
                "--dry-run",
                "test.kicad_pcb",
            ]
        )
        assert args.dry_run is True

    def test_parser_map_flag(self):
        """Parser accepts --map flag for batch mode."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "pcb",
                "move-footprint",
                "--map",
                '{"J2": {"x": 132.5, "y": 98.25}}',
                "test.kicad_pcb",
            ]
        )
        assert args.batch_map == '{"J2": {"x": 132.5, "y": 98.25}}'

    def test_dispatcher_integration(self, tmp_path):
        """Dispatcher correctly routes to move-footprint handler."""
        from kicad_tools.cli.commands.pcb import _run_move_footprint_command

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        class Args:
            ref = "J2"
            to = [132.5, 98.25]
            rotation = None
            batch_map = None
            output = None
            dry_run = True
            format = "text"

        rc = _run_move_footprint_command(Args(), pcb)
        assert rc == 0

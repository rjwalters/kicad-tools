"""Tests for pcb snap-rotation command."""

import json
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB

# Minimal PCB with footprints at various non-cardinal angles
MINIMAL_PCB_ROTATED = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100 15.6)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r2")
    (at 110 100 91.2)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "4.7k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (uuid "fp-c1")
    (at 120 100 179.8)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "100n" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (uuid "fp-c2")
    (at 130 100 315.6)
    (property "Reference" "C2" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10n" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
  (footprint "Package_SO:SOIC-8"
    (layer "F.Cu")
    (uuid "fp-u1")
    (at 140 100 28.5)
    (property "Reference" "U1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "IC" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
)
"""

# PCB with a footprint at 0 degrees (no rotation in at node)
MINIMAL_PCB_ZERO_ROTATION = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
)
"""


def _write_pcb(tmp_path: Path, content: str = MINIMAL_PCB_ROTATED) -> Path:
    """Write a test PCB file and return its path."""
    pcb_file = tmp_path / "test.kicad_pcb"
    pcb_file.write_text(content)
    return pcb_file


class TestSnapRotationBasic:
    """Basic snap-rotation tests."""

    def test_snaps_all_to_cardinal(self, tmp_path):
        """All non-cardinal rotations snap to nearest 90-degree multiple."""
        pcb_file = _write_pcb(tmp_path)
        pcb = PCB.load(pcb_file)

        # Before: non-cardinal angles
        assert pcb.get_footprint("R1").rotation == pytest.approx(15.6)
        assert pcb.get_footprint("R2").rotation == pytest.approx(91.2)
        assert pcb.get_footprint("C1").rotation == pytest.approx(179.8)
        assert pcb.get_footprint("C2").rotation == pytest.approx(315.6)
        assert pcb.get_footprint("U1").rotation == pytest.approx(28.5)

        # Run snap-rotation via the command handler
        from types import SimpleNamespace

        from kicad_tools.cli.commands.pcb import _run_snap_rotation_command

        args = SimpleNamespace(
            pcb=str(pcb_file),
            grid=90.0,
            tolerance=None,
            exclude=None,
            only=None,
            dry_run=False,
            format="text",
            output=None,
        )
        rc = _run_snap_rotation_command(args, pcb_file)
        assert rc == 0

        # Reload and verify
        pcb2 = PCB.load(pcb_file)
        assert pcb2.get_footprint("R1").rotation == pytest.approx(0.0)
        assert pcb2.get_footprint("R2").rotation == pytest.approx(90.0)
        assert pcb2.get_footprint("C1").rotation == pytest.approx(180.0)
        assert pcb2.get_footprint("C2").rotation == pytest.approx(0.0)  # 315.6 -> 360 -> 0
        assert pcb2.get_footprint("U1").rotation == pytest.approx(0.0)

    def test_tolerance_skips_large_delta(self, tmp_path):
        """With --tolerance 10, footprints >10 degrees from grid are not snapped."""
        pcb_file = _write_pcb(tmp_path)
        from types import SimpleNamespace

        from kicad_tools.cli.commands.pcb import _run_snap_rotation_command

        args = SimpleNamespace(
            pcb=str(pcb_file),
            grid=90.0,
            tolerance=10.0,
            exclude=None,
            only=None,
            dry_run=False,
            format="text",
            output=None,
        )
        rc = _run_snap_rotation_command(args, pcb_file)
        assert rc == 0

        pcb2 = PCB.load(pcb_file)
        # R2 at 91.2 -> delta 1.2 < 10 -> snapped to 90
        assert pcb2.get_footprint("R2").rotation == pytest.approx(90.0)
        # C1 at 179.8 -> delta 0.2 < 10 -> snapped to 180
        assert pcb2.get_footprint("C1").rotation == pytest.approx(180.0)
        # R1 at 15.6 -> delta 15.6 > 10 -> NOT snapped
        assert pcb2.get_footprint("R1").rotation == pytest.approx(15.6)
        # U1 at 28.5 -> delta 28.5 > 10 -> NOT snapped
        assert pcb2.get_footprint("U1").rotation == pytest.approx(28.5)

    def test_exclude_refs(self, tmp_path):
        """--exclude skips specified reference designators."""
        pcb_file = _write_pcb(tmp_path)
        from types import SimpleNamespace

        from kicad_tools.cli.commands.pcb import _run_snap_rotation_command

        args = SimpleNamespace(
            pcb=str(pcb_file),
            grid=90.0,
            tolerance=None,
            exclude="U1,R2",
            only=None,
            dry_run=False,
            format="text",
            output=None,
        )
        rc = _run_snap_rotation_command(args, pcb_file)
        assert rc == 0

        pcb2 = PCB.load(pcb_file)
        # U1 and R2 should NOT be snapped
        assert pcb2.get_footprint("U1").rotation == pytest.approx(28.5)
        assert pcb2.get_footprint("R2").rotation == pytest.approx(91.2)
        # Others should be snapped
        assert pcb2.get_footprint("R1").rotation == pytest.approx(0.0)
        assert pcb2.get_footprint("C1").rotation == pytest.approx(180.0)

    def test_only_refs(self, tmp_path):
        """--only limits snapping to specified reference designators."""
        pcb_file = _write_pcb(tmp_path)
        from types import SimpleNamespace

        from kicad_tools.cli.commands.pcb import _run_snap_rotation_command

        args = SimpleNamespace(
            pcb=str(pcb_file),
            grid=90.0,
            tolerance=None,
            exclude=None,
            only="C1,C2",
            dry_run=False,
            format="text",
            output=None,
        )
        rc = _run_snap_rotation_command(args, pcb_file)
        assert rc == 0

        pcb2 = PCB.load(pcb_file)
        # Only C1 and C2 should be snapped
        assert pcb2.get_footprint("C1").rotation == pytest.approx(180.0)
        assert pcb2.get_footprint("C2").rotation == pytest.approx(0.0)
        # Others should NOT be snapped
        assert pcb2.get_footprint("R1").rotation == pytest.approx(15.6)
        assert pcb2.get_footprint("R2").rotation == pytest.approx(91.2)
        assert pcb2.get_footprint("U1").rotation == pytest.approx(28.5)

    def test_dry_run_no_modification(self, tmp_path):
        """--dry-run does not modify the PCB file."""
        pcb_file = _write_pcb(tmp_path)
        original_content = pcb_file.read_text()

        from types import SimpleNamespace

        from kicad_tools.cli.commands.pcb import _run_snap_rotation_command

        args = SimpleNamespace(
            pcb=str(pcb_file),
            grid=90.0,
            tolerance=None,
            exclude=None,
            only=None,
            dry_run=True,
            format="text",
            output=None,
        )
        rc = _run_snap_rotation_command(args, pcb_file)
        assert rc == 0

        # File should be unchanged
        assert pcb_file.read_text() == original_content

    def test_dry_run_json_output(self, tmp_path, capsys):
        """--dry-run with --format json reports changes correctly."""
        pcb_file = _write_pcb(tmp_path)
        from types import SimpleNamespace

        from kicad_tools.cli.commands.pcb import _run_snap_rotation_command

        args = SimpleNamespace(
            pcb=str(pcb_file),
            grid=90.0,
            tolerance=None,
            exclude=None,
            only=None,
            dry_run=True,
            format="json",
            output=None,
        )
        rc = _run_snap_rotation_command(args, pcb_file)
        assert rc == 0

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result["dry_run"] is True
        assert result["snapped"] == 5  # all 5 footprints should be listed
        assert result["output"] is None

    def test_rotation_zero_serialization(self, tmp_path):
        """Snapping to 0 degrees removes the rotation value from (at x y) node."""
        pcb_file = _write_pcb(tmp_path)
        from types import SimpleNamespace

        from kicad_tools.cli.commands.pcb import _run_snap_rotation_command

        args = SimpleNamespace(
            pcb=str(pcb_file),
            grid=90.0,
            tolerance=None,
            exclude=None,
            only="R1",  # R1 is at 15.6 -> snaps to 0
            dry_run=False,
            format="text",
            output=None,
        )
        rc = _run_snap_rotation_command(args, pcb_file)
        assert rc == 0

        # Read the raw file content and verify (at 100 100) has no third value
        content = pcb_file.read_text()
        # Find the R1 footprint's at node -- it should be (at 100 100) not (at 100 100 0)
        import re
        # Find at nodes in the R1 footprint section
        # The first (at ...) after fp-r1 uuid should be the footprint position
        r1_match = re.search(r'uuid "fp-r1"\)\s*\(at ([^)]+)\)', content)
        assert r1_match is not None
        at_values = r1_match.group(1).split()
        # Should have exactly 2 values (x, y) with no rotation
        assert len(at_values) == 2, f"Expected 2 values in (at ...) but got: {at_values}"

    def test_already_cardinal_no_changes(self, tmp_path):
        """Footprints already at cardinal angles produce no changes."""
        pcb_file = _write_pcb(tmp_path, MINIMAL_PCB_ZERO_ROTATION)
        from types import SimpleNamespace

        from kicad_tools.cli.commands.pcb import _run_snap_rotation_command

        args = SimpleNamespace(
            pcb=str(pcb_file),
            grid=90.0,
            tolerance=None,
            exclude=None,
            only=None,
            dry_run=False,
            format="json",
            output=None,
        )
        rc = _run_snap_rotation_command(args, pcb_file)
        assert rc == 0

    def test_output_to_separate_file(self, tmp_path):
        """--output writes to a different file, leaving input unchanged."""
        pcb_file = _write_pcb(tmp_path)
        output_file = tmp_path / "output.kicad_pcb"

        from types import SimpleNamespace

        from kicad_tools.cli.commands.pcb import _run_snap_rotation_command

        args = SimpleNamespace(
            pcb=str(pcb_file),
            grid=90.0,
            tolerance=None,
            exclude=None,
            only=None,
            dry_run=False,
            format="text",
            output=str(output_file),
        )
        rc = _run_snap_rotation_command(args, pcb_file)
        assert rc == 0

        # Output file should exist with snapped rotations
        assert output_file.exists()
        pcb2 = PCB.load(output_file)
        assert pcb2.get_footprint("R1").rotation == pytest.approx(0.0)

    def test_grid_45_degrees(self, tmp_path):
        """Custom grid of 45 degrees works correctly."""
        pcb_file = _write_pcb(tmp_path)
        from types import SimpleNamespace

        from kicad_tools.cli.commands.pcb import _run_snap_rotation_command

        args = SimpleNamespace(
            pcb=str(pcb_file),
            grid=45.0,
            tolerance=None,
            exclude=None,
            only=None,
            dry_run=False,
            format="text",
            output=None,
        )
        rc = _run_snap_rotation_command(args, pcb_file)
        assert rc == 0

        pcb2 = PCB.load(pcb_file)
        # R1 at 15.6 -> nearest 45 multiple is 0 -> 0
        assert pcb2.get_footprint("R1").rotation == pytest.approx(0.0)
        # R2 at 91.2 -> nearest 45 multiple is 90 -> 90
        assert pcb2.get_footprint("R2").rotation == pytest.approx(90.0)
        # C1 at 179.8 -> nearest 45 multiple is 180 -> 180
        assert pcb2.get_footprint("C1").rotation == pytest.approx(180.0)
        # C2 at 315.6 -> nearest 45 multiple is 315 -> 315
        assert pcb2.get_footprint("C2").rotation == pytest.approx(315.0)
        # U1 at 28.5 -> nearest 45 multiple is 45 -> 45
        assert pcb2.get_footprint("U1").rotation == pytest.approx(45.0)


class TestSnapRotationCLIIntegration:
    """Integration tests through the CLI entry point."""

    def test_cli_round_trip(self, tmp_path):
        """Load PCB with non-cardinal angles, run command, reload, verify cardinal."""
        pcb_file = _write_pcb(tmp_path)

        from types import SimpleNamespace

        from kicad_tools.cli.commands.pcb import _run_snap_rotation_command

        args = SimpleNamespace(
            pcb=str(pcb_file),
            grid=90.0,
            tolerance=None,
            exclude=None,
            only=None,
            dry_run=False,
            format="text",
            output=None,
        )
        rc = _run_snap_rotation_command(args, pcb_file)
        assert rc == 0

        # Full round-trip: reload and verify all rotations are cardinal
        pcb = PCB.load(pcb_file)
        for fp in pcb.footprints:
            assert fp.rotation % 90.0 == pytest.approx(0.0), (
                f"{fp.reference} has non-cardinal rotation {fp.rotation}"
            )

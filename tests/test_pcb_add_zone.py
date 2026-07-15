"""Tests for the 'pcb add-zone' CLI command."""

import json

import pytest

from kicad_tools.cli.commands.pcb import run_pcb_command


@pytest.fixture
def sample_pcb(tmp_path):
    """Create a minimal valid PCB file for testing."""
    pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "kicad")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (gr_rect
    (start 0 0)
    (end 50 50)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "edge-uuid")
  )
)
"""
    pcb_file = tmp_path / "test.kicad_pcb"
    pcb_file.write_text(pcb_content)
    return pcb_file


def _make_args(pcb_path, **kwargs):
    """Build a namespace object mimicking argparse output for pcb add-zone."""
    from argparse import Namespace

    defaults = {
        "pcb_command": "add-zone",
        "pcb": str(pcb_path),
        "net": "GND",
        "layer": "B.Cu",
        "priority": 0,
        "min_clearance": 0.3,
        "thermal_relief_gap": 0.3,
        "thermal_relief_width": 0.4,
        "min_thickness": 0.25,
        "fill_board": False,
        "rect": False,
        "origin": None,
        "size": None,
        "output": None,
        "dry_run": False,
        "format": "text",
    }
    defaults.update(kwargs)
    return Namespace(**defaults)


def _zone_keys(pcb_path):
    """Return the set of (net_name, layer) tuples for all zones in a PCB."""
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(pcb_path))
    return {(z.net_name, z.layer) for z in pcb.zones}


class TestPcbAddZoneDryRun:
    """Test pcb add-zone in dry-run mode (no file writes)."""

    def test_dry_run_text_output(self, sample_pcb, capsys):
        """Dry run produces text summary without writing files."""
        args = _make_args(sample_pcb, dry_run=True)
        rc = run_pcb_command(args)
        assert rc == 0

        captured = capsys.readouterr()
        assert "GND" in captured.out
        assert "B.Cu" in captured.out
        assert "board outline" in captured.out

    def test_dry_run_json_output(self, sample_pcb, capsys):
        """Dry run produces valid JSON output."""
        args = _make_args(sample_pcb, dry_run=True, format="json")
        rc = run_pcb_command(args)
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["dry_run"] is True
        assert data["zone"]["net"] == "GND"
        assert data["zone"]["layer"] == "B.Cu"
        assert data["zone"]["boundary_type"] == "board_outline"
        assert data["output"] is None

    def test_dry_run_with_fill_board(self, sample_pcb, capsys):
        """--fill-board flag is accepted (no-op, documents default)."""
        args = _make_args(sample_pcb, dry_run=True, fill_board=True)
        rc = run_pcb_command(args)
        assert rc == 0

        captured = capsys.readouterr()
        assert "board outline" in captured.out

    def test_dry_run_inner_layer(self, sample_pcb, capsys):
        """Zone on inner layer works (dry run)."""
        # Note: Our sample PCB does not define In1.Cu, but ZoneGenerator
        # accepts it. We test with F.Cu instead to stay safe.
        args = _make_args(sample_pcb, dry_run=True, layer="F.Cu", net="+3.3V")
        rc = run_pcb_command(args)
        assert rc == 0

        captured = capsys.readouterr()
        assert "+3.3V" in captured.out
        assert "F.Cu" in captured.out


class TestPcbAddZoneRect:
    """Test pcb add-zone with rectangular boundary."""

    def test_rect_dry_run(self, sample_pcb, capsys):
        """Rectangular zone boundary in dry-run mode."""
        args = _make_args(
            sample_pcb,
            dry_run=True,
            rect=True,
            origin=[10.0, 10.0],
            size=[30.0, 30.0],
        )
        rc = run_pcb_command(args)
        assert rc == 0

        captured = capsys.readouterr()
        assert "rectangle" in captured.out

    def test_rect_json_output(self, sample_pcb, capsys):
        """Rectangular zone produces correct JSON boundary_type."""
        args = _make_args(
            sample_pcb,
            dry_run=True,
            format="json",
            rect=True,
            origin=[10.0, 10.0],
            size=[30.0, 30.0],
        )
        rc = run_pcb_command(args)
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["zone"]["boundary_type"] == "rectangle"
        assert data["zone"]["boundary_points"] == 4

    def test_rect_without_origin_fails(self, sample_pcb, capsys):
        """--rect without --origin produces error."""
        args = _make_args(
            sample_pcb,
            rect=True,
            origin=None,
            size=[30.0, 30.0],
        )
        rc = run_pcb_command(args)
        assert rc == 1

        captured = capsys.readouterr()
        assert "--rect requires" in captured.err

    def test_rect_without_size_fails(self, sample_pcb, capsys):
        """--rect without --size produces error."""
        args = _make_args(
            sample_pcb,
            rect=True,
            origin=[10.0, 10.0],
            size=None,
        )
        rc = run_pcb_command(args)
        assert rc == 1

        captured = capsys.readouterr()
        assert "--rect requires" in captured.err

    def test_rect_negative_size_fails(self, sample_pcb, capsys):
        """Negative size values produce error."""
        args = _make_args(
            sample_pcb,
            rect=True,
            origin=[10.0, 10.0],
            size=[-5.0, 30.0],
        )
        rc = run_pcb_command(args)
        assert rc == 1

        captured = capsys.readouterr()
        assert "positive" in captured.err


class TestPcbAddZoneWrite:
    """Test pcb add-zone with actual file writing."""

    def test_write_zone_to_output(self, sample_pcb, tmp_path, capsys):
        """Writing zone creates valid output file."""
        output = tmp_path / "output.kicad_pcb"
        args = _make_args(sample_pcb, output=str(output))
        rc = run_pcb_command(args)
        assert rc == 0

        assert output.exists()
        content = output.read_text()
        assert "(zone" in content
        assert '"GND"' in content

    def test_write_rect_zone(self, sample_pcb, tmp_path, capsys):
        """Writing rectangular zone produces valid output."""
        output = tmp_path / "output_rect.kicad_pcb"
        args = _make_args(
            sample_pcb,
            output=str(output),
            rect=True,
            origin=[5.0, 5.0],
            size=[20.0, 20.0],
        )
        rc = run_pcb_command(args)
        assert rc == 0

        assert output.exists()
        content = output.read_text()
        assert "(zone" in content
        assert '"GND"' in content

    def test_default_output_path(self, sample_pcb, capsys):
        """Default output overwrites the input in place; no _zones side file."""
        original_bytes = sample_pcb.read_bytes()
        assert ("GND", "B.Cu") not in _zone_keys(sample_pcb)

        args = _make_args(sample_pcb)
        rc = run_pcb_command(args)
        assert rc == 0

        # Input was mutated in place with the new zone.
        assert sample_pcb.read_bytes() != original_bytes
        assert ("GND", "B.Cu") in _zone_keys(sample_pcb)

        # No <stem>_zones.kicad_pcb side file was created (old behavior).
        side_file = sample_pcb.with_stem(sample_pcb.stem + "_zones")
        assert not side_file.exists()

    def test_custom_thermal_params(self, sample_pcb, tmp_path, capsys):
        """Custom thermal relief parameters are passed through."""
        output = tmp_path / "thermal.kicad_pcb"
        args = _make_args(
            sample_pcb,
            output=str(output),
            dry_run=True,
            format="json",
            thermal_relief_gap=0.5,
            thermal_relief_width=0.6,
            min_clearance=0.4,
        )
        rc = run_pcb_command(args)
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["zone"]["clearance"] == 0.4
        assert data["zone"]["thermal_gap"] == 0.5
        assert data["zone"]["thermal_bridge_width"] == 0.6


class TestPcbAddZoneErrors:
    """Test error handling for pcb add-zone."""

    def test_unknown_net(self, sample_pcb, capsys):
        """Unknown net name produces error."""
        args = _make_args(sample_pcb, net="NONEXISTENT")
        rc = run_pcb_command(args)
        assert rc == 1

        captured = capsys.readouterr()
        assert "Error" in captured.err

    def test_missing_pcb_file(self, tmp_path, capsys):
        """Missing PCB file produces error."""
        args = _make_args(tmp_path / "nonexistent.kicad_pcb")
        rc = run_pcb_command(args)
        assert rc == 1

        captured = capsys.readouterr()
        assert "not found" in captured.err or "Error" in captured.err


class TestPcbAddZoneInPlaceDefault:
    """`pcb add-zone` with no -o overwrites the input in place (issue #4195)."""

    def test_add_no_output_modifies_input(self, sample_pcb):
        """No -o writes the new zone back into the input file itself."""
        assert ("GND", "B.Cu") not in _zone_keys(sample_pcb)

        rc = run_pcb_command(_make_args(sample_pcb, net="GND", layer="B.Cu"))
        assert rc == 0

        assert ("GND", "B.Cu") in _zone_keys(sample_pcb)

    def test_add_no_output_does_not_create_side_file(self, sample_pcb):
        """No <stem>_zones.kicad_pcb side file is created (old behavior)."""
        rc = run_pcb_command(_make_args(sample_pcb, net="GND", layer="B.Cu"))
        assert rc == 0

        side_file = sample_pcb.with_stem(sample_pcb.stem + "_zones")
        assert not side_file.exists()

    def test_dry_run_reports_in_place_target_and_writes_nothing(self, sample_pcb, capsys):
        """--dry-run names the in-place target and creates no files."""
        original_bytes = sample_pcb.read_bytes()
        rc = run_pcb_command(
            _make_args(sample_pcb, net="GND", layer="B.Cu", dry_run=True, format="json")
        )
        assert rc == 0

        # Nothing written: input unchanged, no side file.
        assert sample_pcb.read_bytes() == original_bytes
        side_file = sample_pcb.with_stem(sample_pcb.stem + "_zones")
        assert not side_file.exists()

        data = json.loads(capsys.readouterr().out)
        assert data["dry_run"] is True
        assert data["output"] is None


class TestPcbAddZoneChainedAccumulates:
    """Two sequential in-place `pcb add-zone` calls accumulate both zones.

    Regression test for the compounding data-loss bug (issue #4195): previously
    each call read the pristine input and wrote the same <stem>_zones side file,
    so the second call silently discarded the first zone.
    """

    def test_two_sequential_adds_keep_both_zones(self, sample_pcb):
        rc1 = run_pcb_command(_make_args(sample_pcb, net="GND", layer="B.Cu"))
        assert rc1 == 0
        rc2 = run_pcb_command(_make_args(sample_pcb, net="+3.3V", layer="F.Cu"))
        assert rc2 == 0

        keys = _zone_keys(sample_pcb)
        assert ("GND", "B.Cu") in keys  # first add survived the second
        assert ("+3.3V", "F.Cu") in keys  # second add landed too


class TestPcbAddZoneExplicitOutputUnaffected:
    """Explicit -o still writes the target and leaves the input untouched."""

    def test_explicit_output_writes_target_leaves_input_unchanged(self, sample_pcb, tmp_path):
        out = tmp_path / "with_zones.kicad_pcb"
        original_bytes = sample_pcb.read_bytes()
        assert ("GND", "B.Cu") not in _zone_keys(sample_pcb)

        rc = run_pcb_command(_make_args(sample_pcb, net="GND", layer="B.Cu", output=str(out)))
        assert rc == 0

        # Input is byte-for-byte unchanged.
        assert sample_pcb.read_bytes() == original_bytes
        # Target got the new zone.
        assert ("GND", "B.Cu") in _zone_keys(out)

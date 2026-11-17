"""Tests for the ``pcb zones`` subcommand."""

import json
import shutil
from pathlib import Path

import pytest

from kicad_tools.cli.commands.pcb import run_pcb_command

FIXTURE_PCB = Path(__file__).parent / "fixtures" / "projects" / "multilayer_zones.kicad_pcb"


class _Args:
    """Minimal namespace to mimic argparse output for run_pcb_command."""

    def __init__(self, pcb: str, fmt: str = "text"):
        self.pcb_command = "zones"
        self.pcb = pcb
        self.format = fmt


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_pcb(tmp_path: Path) -> Path:
    """Copy the multilayer_zones fixture into a temp directory."""
    dest = tmp_path / "board.kicad_pcb"
    shutil.copy2(FIXTURE_PCB, dest)
    return dest


@pytest.fixture
def empty_pcb(tmp_path: Path) -> Path:
    """Create a minimal PCB with no zones."""
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.create(width=50, height=50)
    pcb_path = tmp_path / "empty.kicad_pcb"
    pcb.save(str(pcb_path))
    return pcb_path


# ---------------------------------------------------------------------------
# JSON output tests
# ---------------------------------------------------------------------------


class TestPcbZonesJson:
    """Verify JSON output from ``pcb zones``."""

    def test_json_output_has_required_fields(self, tmp_pcb, capsys):
        """JSON output includes all required fields including fill_type and bounding_box."""
        args = _Args(str(tmp_pcb), "json")
        ret = run_pcb_command(args)
        assert ret == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "zones" in data
        assert "count" in data
        assert data["count"] == len(data["zones"])
        assert data["count"] > 0, "Fixture must have at least one zone"

        for zone in data["zones"]:
            # Core fields
            assert "net_number" in zone
            assert "net_name" in zone
            assert "layer" in zone
            assert "priority" in zone
            assert "clearance" in zone
            assert "thermal_gap" in zone
            assert "thermal_bridge_width" in zone
            assert "is_filled" in zone
            assert "boundary_points" in zone
            # New fields
            assert "fill_type" in zone
            assert zone["fill_type"] in ("solid", "hatch")
            assert "bounding_box" in zone

    def test_json_bounding_box_structure(self, tmp_pcb, capsys):
        """Bounding box has min_x, min_y, max_x, max_y keys."""
        args = _Args(str(tmp_pcb), "json")
        run_pcb_command(args)

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        for zone in data["zones"]:
            bb = zone["bounding_box"]
            if bb is not None:
                assert "min_x" in bb
                assert "min_y" in bb
                assert "max_x" in bb
                assert "max_y" in bb
                assert bb["max_x"] >= bb["min_x"]
                assert bb["max_y"] >= bb["min_y"]

    def test_json_empty_pcb(self, empty_pcb, capsys):
        """An empty PCB returns count 0 and an empty zones list."""
        args = _Args(str(empty_pcb), "json")
        ret = run_pcb_command(args)
        assert ret == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data == {"zones": [], "count": 0}


# ---------------------------------------------------------------------------
# Text output tests
# ---------------------------------------------------------------------------


class TestPcbZonesText:
    """Verify text output from ``pcb zones``."""

    def test_text_output_shows_zones(self, tmp_pcb, capsys):
        """Text output includes zone details."""
        args = _Args(str(tmp_pcb), "text")
        ret = run_pcb_command(args)
        assert ret == 0

        captured = capsys.readouterr()
        assert "zone(s)" in captured.out.lower() or "Zone" in captured.out
        assert "Net:" in captured.out
        assert "Layer:" in captured.out
        assert "Fill type:" in captured.out
        assert "Bounds:" in captured.out

    def test_text_empty_pcb(self, empty_pcb, capsys):
        """An empty PCB prints 'No zones found'."""
        args = _Args(str(empty_pcb), "text")
        ret = run_pcb_command(args)
        assert ret == 0

        captured = capsys.readouterr()
        assert "No zones found" in captured.out


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestPcbZonesErrors:
    """Verify error handling for ``pcb zones``."""

    def test_missing_file_returns_1(self, capsys):
        """Non-existent file returns exit code 1."""
        args = _Args("/nonexistent/board.kicad_pcb", "text")
        ret = run_pcb_command(args)
        assert ret == 1

        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "Error" in captured.err


# ---------------------------------------------------------------------------
# Bounding box calculation
# ---------------------------------------------------------------------------


class TestBoundingBoxCalculation:
    """Unit tests for bounding box computation from polygon points."""

    def test_bounding_box_from_square(self, tmp_pcb, capsys):
        """Bounding box is correctly computed from zone polygon."""
        args = _Args(str(tmp_pcb), "json")
        run_pcb_command(args)

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # Verify at least one zone has a non-null bounding box with valid extents
        has_bbox = False
        for zone in data["zones"]:
            bb = zone["bounding_box"]
            if bb is not None:
                has_bbox = True
                # Width and height must be positive (non-degenerate)
                width = bb["max_x"] - bb["min_x"]
                height = bb["max_y"] - bb["min_y"]
                assert width > 0, "Bounding box width must be positive"
                assert height > 0, "Bounding box height must be positive"

        assert has_bbox, "At least one zone should have a bounding box"


# ---------------------------------------------------------------------------
# Regression: top-level zones list still works
# ---------------------------------------------------------------------------


class TestTopLevelZonesListRegression:
    """Verify the top-level ``zones list`` command still works."""

    def test_zones_list_json(self, tmp_pcb, capsys):
        """Top-level zones list JSON output still includes fill_type and bounding_box."""
        from kicad_tools.cli.zones_cmd import main as zones_main

        ret = zones_main(["list", str(tmp_pcb), "--format", "json"])
        assert ret == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "zones" in data
        assert data["count"] > 0

        for zone in data["zones"]:
            assert "fill_type" in zone
            assert "bounding_box" in zone

    def test_zones_list_text(self, tmp_pcb, capsys):
        """Top-level zones list text output still works."""
        from kicad_tools.cli.zones_cmd import main as zones_main

        ret = zones_main(["list", str(tmp_pcb)])
        assert ret == 0

        captured = capsys.readouterr()
        assert "zone(s)" in captured.out.lower() or "Zone" in captured.out

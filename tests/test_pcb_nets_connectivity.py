"""Tests for pcb nets --check-connectivity flag."""

import json
from pathlib import Path

import pytest

from kicad_tools.cli.pcb_query import main as pcb_query_main

# PCB with fully connected nets (all pads linked by traces)
CONNECTED_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")

  (gr_rect
    (start 0 0)
    (end 30 30)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )

  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "R1")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 2 "GND"))
  )

  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 20 10)
    (property "Reference" "R2")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 2 "GND"))
  )

  (segment (start 9.5 10) (end 19.5 10) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 10.5 10) (end 20.5 10) (width 0.25) (layer "F.Cu") (net 2))
)
"""

# PCB with disconnected islands: net 1 has two pads connected, one isolated
DISCONNECTED_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "SCL")

  (gr_rect
    (start 0 0)
    (end 50 50)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )

  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "R1")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 1 "SCL"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 0 ""))
  )

  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 20 10)
    (property "Reference" "R2")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 1 "SCL"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 0 ""))
  )

  (footprint "Package_SO:SOIC-8"
    (layer "F.Cu")
    (at 40 10)
    (property "Reference" "U1")
    (pad "5" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 1 "SCL"))
  )

  (segment (start 9.5 10) (end 19.5 10) (width 0.25) (layer "F.Cu") (net 1))
)
"""

# PCB with a single-pad net (should report 1 island)
SINGLE_PAD_NET_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "SINGLE")

  (gr_rect
    (start 0 0)
    (end 30 30)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )

  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "R1")
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 1 "SINGLE"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 0 ""))
  )
)
"""

# PCB with unrouted net (0 pads on net = 0 islands)
ZERO_PAD_NET_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "ORPHAN")

  (gr_rect
    (start 0 0)
    (end 30 30)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )
)
"""


@pytest.fixture
def connected_pcb(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "connected.kicad_pcb"
    pcb_file.write_text(CONNECTED_PCB)
    return pcb_file


@pytest.fixture
def disconnected_pcb(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "disconnected.kicad_pcb"
    pcb_file.write_text(DISCONNECTED_PCB)
    return pcb_file


@pytest.fixture
def single_pad_net_pcb(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "single_pad.kicad_pcb"
    pcb_file.write_text(SINGLE_PAD_NET_PCB)
    return pcb_file


@pytest.fixture
def zero_pad_net_pcb(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "zero_pad.kicad_pcb"
    pcb_file.write_text(ZERO_PAD_NET_PCB)
    return pcb_file


class TestCheckConnectivityJSON:
    """Test --check-connectivity with JSON output."""

    def test_connected_nets_report_one_island(self, connected_pcb: Path, capsys):
        """Fully connected nets should have island_count=1 and is_complete=True."""
        pcb_query_main([str(connected_pcb), "nets", "--check-connectivity", "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        for net in data:
            assert "island_count" in net
            assert "islands" in net
            assert "is_complete" in net
            assert net["island_count"] == 1
            assert net["is_complete"] is True

    def test_disconnected_net_reports_multiple_islands(self, disconnected_pcb: Path, capsys):
        """A net with disconnected segments should report island_count > 1."""
        pcb_query_main([str(disconnected_pcb), "nets", "--check-connectivity", "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # Find net SCL
        scl_nets = [n for n in data if n["name"] == "SCL"]
        assert len(scl_nets) == 1
        scl = scl_nets[0]
        assert scl["island_count"] == 2
        assert scl["is_complete"] is False
        # Verify island membership: R1.1 and R2.1 are connected, U1.5 is isolated
        all_pads = []
        for island in scl["islands"]:
            all_pads.extend(island)
        assert "U1.5" in all_pads
        assert "R1.1" in all_pads
        assert "R2.1" in all_pads

    def test_single_pad_net_reports_one_island(self, single_pad_net_pcb: Path, capsys):
        """A single-pad net should report island_count=1."""
        pcb_query_main(
            [str(single_pad_net_pcb), "nets", "--check-connectivity", "--format", "json"]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        single_nets = [n for n in data if n["name"] == "SINGLE"]
        assert len(single_nets) == 1
        assert single_nets[0]["island_count"] == 1
        assert single_nets[0]["is_complete"] is True

    def test_zero_pad_net_reports_zero_islands(self, zero_pad_net_pcb: Path, capsys):
        """A net with no pads should report island_count=0."""
        pcb_query_main([str(zero_pad_net_pcb), "nets", "--check-connectivity", "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        orphan_nets = [n for n in data if n["name"] == "ORPHAN"]
        assert len(orphan_nets) == 1
        assert orphan_nets[0]["island_count"] == 0
        assert orphan_nets[0]["is_complete"] is True  # 0 islands <= 1


class TestCheckConnectivityText:
    """Test --check-connectivity with text output."""

    def test_text_output_includes_islands_column(self, connected_pcb: Path, capsys):
        """Text output should include Islands column when flag is set."""
        pcb_query_main([str(connected_pcb), "nets", "--check-connectivity"])

        captured = capsys.readouterr()
        assert "Islands" in captured.out

    def test_text_output_shows_island_detail_for_disconnected(self, disconnected_pcb: Path, capsys):
        """Disconnected nets should show island membership in text output."""
        pcb_query_main([str(disconnected_pcb), "nets", "--check-connectivity"])

        captured = capsys.readouterr()
        assert "2 islands" in captured.out
        assert "U1.5" in captured.out


class TestWithoutCheckConnectivity:
    """Verify backward compatibility when --check-connectivity is not set."""

    def test_json_output_unchanged(self, connected_pcb: Path, capsys):
        """JSON output without --check-connectivity should not include island fields."""
        pcb_query_main([str(connected_pcb), "nets", "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        for net in data:
            assert "island_count" not in net
            assert "islands" not in net
            assert "is_complete" not in net

    def test_text_output_unchanged(self, connected_pcb: Path, capsys):
        """Text output without --check-connectivity should not show Islands column."""
        pcb_query_main([str(connected_pcb), "nets"])

        captured = capsys.readouterr()
        assert "Islands" not in captured.out


class TestCLIFlagWiring:
    """Test that --check-connectivity flag is properly wired through the CLI."""

    def test_flag_accepted_by_pcb_query(self, connected_pcb: Path, capsys):
        """pcb_query should accept --check-connectivity without error."""
        # Should not raise SystemExit
        pcb_query_main([str(connected_pcb), "nets", "--check-connectivity"])
        captured = capsys.readouterr()
        assert "Total:" in captured.out

    def test_flag_combined_with_filter(self, disconnected_pcb: Path, capsys):
        """--check-connectivity should work with --filter."""
        pcb_query_main(
            [
                str(disconnected_pcb),
                "nets",
                "--check-connectivity",
                "--filter",
                "SCL",
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data) == 1
        assert data[0]["name"] == "SCL"
        assert data[0]["island_count"] == 2

    def test_flag_combined_with_sorted(self, connected_pcb: Path, capsys):
        """--check-connectivity should work with --sorted."""
        pcb_query_main(
            [str(connected_pcb), "nets", "--check-connectivity", "--sorted", "--format", "json"]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        names = [n["name"] for n in data]
        assert names == sorted(names)

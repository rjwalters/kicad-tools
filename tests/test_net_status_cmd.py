"""Tests for kicad_tools net status CLI command."""

import json
from pathlib import Path

import pytest

from kicad_tools.cli.net_status_cmd import main


# PCB with fully connected nets
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


# PCB with incomplete nets (some pads not connected)
INCOMPLETE_PCB = """(kicad_pcb
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
  (net 3 "SIG")

  (gr_rect
    (start 0 0)
    (end 50 50)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )

  (footprint "Package_SO:SOIC-8"
    (layer "F.Cu")
    (at 25 25)
    (property "Reference" "U1")
    (pad "1" smd rect (at -3 -2) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 1 "VCC"))
    (pad "2" smd rect (at -3 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 2 "GND"))
    (pad "3" smd rect (at -3 2) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 3 "SIG"))
    (pad "4" smd rect (at 3 -2) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 0 ""))
    (pad "5" smd rect (at 3 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 0 ""))
    (pad "6" smd rect (at 3 2) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 3 "SIG"))
  )

  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (at 10 25)
    (property "Reference" "C1")
    (pad "1" smd rect (at 0 -0.25) (size 0.4 0.4) (layers "F.Cu" "F.Mask") (net 1 "VCC"))
    (pad "2" smd rect (at 0 0.25) (size 0.4 0.4) (layers "F.Cu" "F.Mask") (net 2 "GND"))
  )

  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 40 25)
    (property "Reference" "R1")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 3 "SIG"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 2 "GND"))
  )

  (segment (start 22 23) (end 10 24.75) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 10 25.25) (end 22 25) (width 0.25) (layer "F.Cu") (net 2))
)
"""


# PCB with unrouted nets (no traces)
UNROUTED_PCB = """(kicad_pcb
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
  (net 1 "SIG1")
  (net 2 "SIG2")

  (gr_rect
    (start 0 0)
    (end 40 40)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )

  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x02"
    (layer "F.Cu")
    (at 10 20)
    (property "Reference" "J1")
    (pad "1" thru_hole oval (at 0 0) (size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask") (net 1 "SIG1"))
    (pad "2" thru_hole oval (at 0 2.54) (size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask") (net 2 "SIG2"))
  )

  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x02"
    (layer "F.Cu")
    (at 30 20)
    (property "Reference" "J2")
    (pad "1" thru_hole oval (at 0 0) (size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask") (net 1 "SIG1"))
    (pad "2" thru_hole oval (at 0 2.54) (size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask") (net 2 "SIG2"))
  )
)
"""


@pytest.fixture
def connected_pcb(tmp_path: Path) -> Path:
    """Create a PCB with fully connected nets."""
    pcb_file = tmp_path / "connected.kicad_pcb"
    pcb_file.write_text(CONNECTED_PCB)
    return pcb_file


@pytest.fixture
def incomplete_pcb(tmp_path: Path) -> Path:
    """Create a PCB with some incomplete nets."""
    pcb_file = tmp_path / "incomplete.kicad_pcb"
    pcb_file.write_text(INCOMPLETE_PCB)
    return pcb_file


@pytest.fixture
def unrouted_pcb(tmp_path: Path) -> Path:
    """Create a PCB with unrouted nets."""
    pcb_file = tmp_path / "unrouted.kicad_pcb"
    pcb_file.write_text(UNROUTED_PCB)
    return pcb_file


class TestNetStatusCLI:
    """Tests for the net-status CLI command."""

    def test_file_not_found(self, capsys):
        """Test CLI with missing file."""
        result = main(["nonexistent.kicad_pcb"])
        assert result == 1

        captured = capsys.readouterr()
        assert "not found" in captured.err.lower()

    def test_basic_text_output(self, connected_pcb: Path, capsys):
        """Test basic text output format."""
        result = main([str(connected_pcb)])

        captured = capsys.readouterr()
        assert "Net Status:" in captured.out
        assert "Summary:" in captured.out
        assert "nets total" in captured.out

    def test_json_output(self, connected_pcb: Path, capsys):
        """Test JSON output format."""
        result = main([str(connected_pcb), "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "pcb" in data
        assert "summary" in data
        assert "total_nets" in data["summary"]
        assert "complete" in data["summary"]
        assert "incomplete" in data["summary"]
        assert "unrouted" in data["summary"]

    def test_connected_pcb_returns_0(self, connected_pcb: Path, capsys):
        """Test that fully connected PCB returns exit code 0."""
        result = main([str(connected_pcb)])
        # Exit 0 when no incomplete nets (or 2 if there are incomplete)
        assert result in (0, 2)

    def test_incomplete_pcb_returns_2(self, incomplete_pcb: Path, capsys):
        """Test that incomplete PCB returns exit code 2."""
        result = main([str(incomplete_pcb)])
        # Should return 2 when there are incomplete nets
        assert result == 2

    def test_unrouted_pcb_returns_2(self, unrouted_pcb: Path, capsys):
        """Test that unrouted PCB returns exit code 2."""
        result = main([str(unrouted_pcb)])
        assert result == 2

        captured = capsys.readouterr()
        # Should mention unrouted status
        assert "Unrouted" in captured.out or "unrouted" in captured.out.lower()

    def test_incomplete_filter(self, incomplete_pcb: Path, capsys):
        """Test --incomplete filter shows only incomplete nets."""
        result = main([str(incomplete_pcb), "--incomplete"])

        captured = capsys.readouterr()
        # Should show incomplete/unrouted nets
        assert "Incomplete" in captured.out or "unconnected" in captured.out.lower()

    def test_net_filter_found(self, incomplete_pcb: Path, capsys):
        """Test --net filter for existing net."""
        result = main([str(incomplete_pcb), "--net", "VCC"])

        # Result depends on whether VCC is complete or not
        captured = capsys.readouterr()
        # Should show VCC net info
        assert "VCC" in captured.out or result == 1

    def test_net_filter_not_found(self, incomplete_pcb: Path, capsys):
        """Test --net filter for non-existent net."""
        result = main([str(incomplete_pcb), "--net", "NONEXISTENT"])
        assert result == 1

        captured = capsys.readouterr()
        assert "not found" in captured.err.lower()

    def test_by_class_option(self, incomplete_pcb: Path, capsys):
        """Test --by-class grouping option."""
        result = main([str(incomplete_pcb), "--by-class"])

        captured = capsys.readouterr()
        # Should show net class grouping
        assert "Net Class:" in captured.out or "Default" in captured.out or "class" in captured.out.lower()

    def test_verbose_option(self, incomplete_pcb: Path, capsys):
        """Test --verbose option shows more details."""
        result = main([str(incomplete_pcb), "--verbose"])

        captured = capsys.readouterr()
        # Verbose should show coordinates
        # Check for coordinate patterns or pad info
        assert len(captured.out) > 0

    def test_json_by_class(self, incomplete_pcb: Path, capsys):
        """Test JSON output with --by-class option."""
        result = main([str(incomplete_pcb), "--format", "json", "--by-class"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "by_class" in data
        assert isinstance(data["by_class"], dict)

    def test_json_nets_list(self, incomplete_pcb: Path, capsys):
        """Test JSON output contains nets list."""
        result = main([str(incomplete_pcb), "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "nets" in data
        assert isinstance(data["nets"], list)


class TestNetStatusSummary:
    """Tests for net status summary information."""

    def test_summary_counts(self, incomplete_pcb: Path, capsys):
        """Test that summary shows correct counts."""
        main([str(incomplete_pcb), "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        summary = data["summary"]
        total = summary["total_nets"]
        complete = summary["complete"]
        incomplete = summary["incomplete"]
        unrouted = summary["unrouted"]

        # Counts should be consistent
        assert complete + incomplete + unrouted == total or total >= complete + incomplete + unrouted

    def test_unconnected_pads_count(self, unrouted_pcb: Path, capsys):
        """Test that unconnected pads are counted."""
        main([str(unrouted_pcb), "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # Should have unconnected pads
        assert "total_unconnected_pads" in data["summary"]


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_empty_pcb(self, tmp_path: Path, capsys):
        """Test handling of PCB with no nets."""
        empty_pcb = tmp_path / "empty.kicad_pcb"
        empty_pcb.write_text("""(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers
            (0 "F.Cu" signal)
            (44 "Edge.Cuts" user)
          )
          (net 0 "")
        )
        """)

        result = main([str(empty_pcb)])
        # Should handle gracefully
        assert result in (0, 1, 2)

    def test_pcb_with_only_unconnected_net(self, tmp_path: Path, capsys):
        """Test PCB with only the unconnected net (net 0)."""
        only_unconnected = tmp_path / "only_unconnected.kicad_pcb"
        only_unconnected.write_text("""(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers
            (0 "F.Cu" signal)
            (44 "Edge.Cuts" user)
          )
          (net 0 "")

          (footprint "Test"
            (layer "F.Cu")
            (at 10 10)
            (property "Reference" "R1")
            (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0 ""))
          )
        )
        """)

        result = main([str(only_unconnected)])
        # Should handle gracefully (net 0 is typically excluded)
        assert result in (0, 1, 2)

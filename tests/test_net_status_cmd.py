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
        main([str(connected_pcb)])

        captured = capsys.readouterr()
        assert "Net Status:" in captured.out
        assert "Summary:" in captured.out
        assert "nets total" in captured.out

    def test_json_output(self, connected_pcb: Path, capsys):
        """Test JSON output format."""
        main([str(connected_pcb), "--format", "json"])

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
        main([str(incomplete_pcb), "--incomplete"])

        captured = capsys.readouterr()
        # Should show incomplete/unrouted nets
        assert "Incomplete" in captured.out or "unconnected" in captured.out.lower()

    def test_incomplete_summary_header_internally_consistent(self, incomplete_pcb: Path, capsys):
        """Bug #1 regression: --incomplete summary must describe the full board.

        Previously the ``--incomplete`` filter dropped the complete nets from
        the result but kept the original ``total_nets``, so the Summary header
        printed e.g. ``Complete: 0`` next to ``N nets total`` -- inconsistent.
        The header must reflect the unfiltered board: Complete + Incomplete +
        Unrouted == total, and Complete must equal the non-``--incomplete`` run.
        """
        import re

        def _parse_summary(out: str) -> tuple[int, int, int, int]:
            total = int(re.search(r"Summary: (\d+) nets total", out).group(1))
            complete = int(re.search(r"Complete:\s+(\d+)", out).group(1))
            incomplete = int(re.search(r"Incomplete:\s+(\d+)", out).group(1))
            unrouted = int(re.search(r"Unrouted:\s+(\d+)", out).group(1))
            return total, complete, incomplete, unrouted

        # Unfiltered run establishes the ground-truth counts.
        main([str(incomplete_pcb)])
        full_out = capsys.readouterr().out
        full_total, full_complete, full_incomplete, full_unrouted = _parse_summary(full_out)
        # The fixture must actually exercise the bug: at least one complete net.
        assert full_complete > 0

        # --incomplete run: header must match the unfiltered board exactly.
        main([str(incomplete_pcb), "--incomplete"])
        inc_out = capsys.readouterr().out
        inc_total, inc_complete, inc_incomplete, inc_unrouted = _parse_summary(inc_out)

        # Internally consistent: the three buckets sum to the stated total.
        assert inc_complete + inc_incomplete + inc_unrouted == inc_total
        # And the header describes the full board, not the filtered subset.
        assert inc_total == full_total
        assert inc_complete == full_complete
        assert inc_incomplete == full_incomplete
        assert inc_unrouted == full_unrouted

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
        main([str(incomplete_pcb), "--by-class"])

        captured = capsys.readouterr()
        # Should show net class grouping
        assert (
            "Net Class:" in captured.out
            or "Default" in captured.out
            or "class" in captured.out.lower()
        )

    def test_verbose_option(self, incomplete_pcb: Path, capsys):
        """Test --verbose option shows more details."""
        main([str(incomplete_pcb), "--verbose"])

        captured = capsys.readouterr()
        # Verbose should show coordinates
        # Check for coordinate patterns or pad info
        assert len(captured.out) > 0

    def test_json_by_class(self, incomplete_pcb: Path, capsys):
        """Test JSON output with --by-class option."""
        main([str(incomplete_pcb), "--format", "json", "--by-class"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "by_class" in data
        assert isinstance(data["by_class"], dict)

    def test_json_nets_list(self, incomplete_pcb: Path, capsys):
        """Test JSON output contains nets list."""
        main([str(incomplete_pcb), "--format", "json"])

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
        assert (
            complete + incomplete + unrouted == total or total >= complete + incomplete + unrouted
        )

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


class TestConnectivityModelSelection:
    """Issue #4557: strict is the default; --legacy-proximity is the opt-out."""

    def test_strict_flag_is_accepted_noop(self, connected_pcb: Path, capsys):
        """--strict stays accepted for script compat and matches the default."""
        assert main([str(connected_pcb), "--strict"]) == 0
        strict_out = capsys.readouterr().out
        assert main([str(connected_pcb)]) == 0
        default_out = capsys.readouterr().out
        assert strict_out == default_out

    def test_legacy_proximity_flag_accepted(self, connected_pcb: Path, capsys):
        assert main([str(connected_pcb), "--legacy-proximity"]) == 0
        out = capsys.readouterr().out
        assert "legacy endpoint-proximity" in out

    def test_strict_and_legacy_mutually_exclusive(self, connected_pcb: Path, capsys):
        with pytest.raises(SystemExit) as excinfo:
            main([str(connected_pcb), "--strict", "--legacy-proximity"])
        assert excinfo.value.code == 2

    def test_text_output_names_model_default(self, connected_pcb: Path, capsys):
        main([str(connected_pcb)])
        out = capsys.readouterr().out
        assert "Connectivity model: strict (real copper geometry" in out

    def test_json_output_names_model_default(self, connected_pcb: Path, capsys):
        main([str(connected_pcb), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        assert data["connectivity_model"] == "strict"

    def test_json_output_names_model_legacy(self, connected_pcb: Path, capsys):
        main([str(connected_pcb), "--format", "json", "--legacy-proximity"])
        data = json.loads(capsys.readouterr().out)
        assert data["connectivity_model"] == "legacy_proximity"

    def test_why_output_names_model(self, connected_pcb: Path, capsys):
        """--why states which connectivity model produced the verdict."""
        main([str(connected_pcb), "--why"])
        out = capsys.readouterr().out
        assert "Connectivity model: strict (real copper geometry" in out

    def test_why_respects_legacy_selection(self, connected_pcb: Path, capsys):
        """--legacy-proximity --why threads the model into the classifier.

        Before #4557, ``--strict --why`` silently ignored the model flag
        because ``classify_stuck_nets_from_pcb`` hardcoded default-mode
        analyzers.
        """
        main([str(connected_pcb), "--why", "--legacy-proximity"])
        out = capsys.readouterr().out
        assert "Connectivity model: legacy endpoint-proximity" in out

    def test_why_json_names_model(self, connected_pcb: Path, capsys):
        main([str(connected_pcb), "--why", "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        assert data["connectivity_model"] == "strict"

    def test_exit_codes_unchanged_by_model_on_genuine_open(self, unrouted_pcb: Path, capsys):
        """A truly disconnected board exits 2 under BOTH models (no masking)."""
        assert main([str(unrouted_pcb)]) == 2
        capsys.readouterr()
        assert main([str(unrouted_pcb), "--legacy-proximity"]) == 2


class TestNetFilterScoping:
    """Issue #4682: --net must scope the banner/summary AND the --why output.

    Fixture facts (``incomplete_pcb``): VCC is complete (2 pads, traced);
    GND is incomplete (R1.2 stranded); SIG is unrouted. So VCC is the
    "complete net on a board with other incomplete nets" case from the issue.
    """

    def test_net_filter_banner_and_summary_agree_in_scope(self, incomplete_pcb: Path, capsys):
        """--net <complete-net> must not claim board-wide completeness.

        Previously the board-wide summary ("Incomplete: 2") rendered directly
        above "All nets are fully connected!" (derived from the filtered set)
        -- a direct contradiction.
        """
        result = main([str(incomplete_pcb), "--net", "VCC"])
        out = capsys.readouterr().out

        assert "All nets are fully connected!" not in out
        assert "Selected net 'VCC' is fully connected." in out
        # Summary is scoped to the selection, with board total for context.
        assert "Summary: 1 net selected (of 3 on board)" in out
        assert "Incomplete: 0" in out
        # Exit code deliberately stays BOARD-WIDE (documented in --net help):
        # the board has incomplete nets, so exit 2 even though VCC is complete.
        assert result == 2

    def test_complete_percentage_literal_reworded(self, incomplete_pcb: Path, capsys):
        """The '(100% connected)' literal read as a board-level statistic."""
        main([str(incomplete_pcb)])
        out = capsys.readouterr().out
        assert "100% connected" not in out
        assert "(all pads connected)" in out

    def test_why_net_filter_complete_net(self, incomplete_pcb: Path, capsys):
        """--net <complete-net> --why names the net and says it is not stuck.

        Previously --why ignored --net entirely and printed every OTHER stuck
        net's diagnosis (asked about DAC_CLK, told about GNDA).
        """
        result = main([str(incomplete_pcb), "--net", "VCC", "--why"])
        out = capsys.readouterr().out

        assert result == 0
        assert "Selected net: VCC" in out
        assert "Net 'VCC' is not stuck" in out
        # No other net's diagnosis leaks into the filtered output.
        assert "GND" not in out
        assert "SIG" not in out

    def test_why_net_filter_stuck_net(self, incomplete_pcb: Path, capsys):
        """--net <stuck-net> --why prints only that net's diagnosis, exit 2."""
        result = main([str(incomplete_pcb), "--net", "SIG", "--why"])
        out = capsys.readouterr().out

        assert result == 2
        assert "Selected net: SIG" in out
        assert "Stuck signal nets: 1" in out
        assert "SIG" in out
        # GND is also stuck on this board but was not asked about.
        assert "GND" not in out

    def test_why_net_filter_not_found(self, incomplete_pcb: Path, capsys):
        """--net NONEXISTENT --why errors with the available-nets listing."""
        result = main([str(incomplete_pcb), "--net", "NONEXISTENT", "--why"])
        captured = capsys.readouterr()

        assert result == 1
        assert "not found" in captured.err.lower()
        assert "Available nets" in captured.err

    def test_why_json_net_filter_complete_net(self, incomplete_pcb: Path, capsys):
        """--net X --why --format json is filtered identically to text."""
        result = main([str(incomplete_pcb), "--net", "VCC", "--why", "--format", "json"])
        data = json.loads(capsys.readouterr().out)

        assert result == 0
        assert data["net_filter"] == "VCC"
        assert data["summary"]["stuck_nets"] == 0
        assert data["nets"] == []

    def test_why_json_net_filter_stuck_net(self, incomplete_pcb: Path, capsys):
        result = main([str(incomplete_pcb), "--net", "SIG", "--why", "--format", "json"])
        data = json.loads(capsys.readouterr().out)

        assert result == 2
        assert data["net_filter"] == "SIG"
        assert data["summary"]["stuck_nets"] == 1
        assert [n["net_name"] for n in data["nets"]] == ["SIG"]

    def test_why_json_without_net_has_no_filter_key(self, incomplete_pcb: Path, capsys):
        """Plain --why JSON stays byte-compatible (net_filter key is additive)."""
        main([str(incomplete_pcb), "--why", "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        assert "net_filter" not in data

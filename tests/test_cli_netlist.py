"""Tests for netlist CLI commands."""

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from kicad_tools.cli import netlist_cmd
from kicad_tools.operations.netlist import Netlist, NetlistComponent, NetlistNet, NetNode
from kicad_tools.sexp import parse_sexp


class TestNetNodeFromSexp:
    """Tests for NetNode.from_sexp() parsing."""

    def test_from_sexp_parses_reference_from_ref_node(self):
        """Test that reference is correctly parsed from (ref ...) child node.

        This tests the fix for issue #923 where NetNode.from_sexp() was
        incorrectly trying to get the reference as a direct string value
        instead of from the (ref ...) child node.
        """
        sexp = parse_sexp('(node (ref "R1") (pin "1"))')
        node = NetNode.from_sexp(sexp)

        assert node.reference == "R1"
        assert node.pin == "1"

    def test_from_sexp_parses_all_fields(self):
        """Test that all node fields are correctly parsed."""
        sexp = parse_sexp('(node (ref "U1") (pin "3") (pinfunction "VCC") (pintype "power_in"))')
        node = NetNode.from_sexp(sexp)

        assert node.reference == "U1"
        assert node.pin == "3"
        assert node.pin_function == "VCC"
        assert node.pin_type == "power_in"

    def test_from_sexp_handles_missing_optional_fields(self):
        """Test parsing when optional fields are missing."""
        sexp = parse_sexp('(node (ref "C1") (pin "2"))')
        node = NetNode.from_sexp(sexp)

        assert node.reference == "C1"
        assert node.pin == "2"
        assert node.pin_function == ""
        assert node.pin_type == ""

    def test_from_sexp_handles_empty_node(self):
        """Test parsing an empty node."""
        sexp = parse_sexp("(node)")
        node = NetNode.from_sexp(sexp)

        assert node.reference == ""
        assert node.pin == ""


class TestNetlistCmdHelpers:
    """Tests for netlist_cmd helper functions."""

    def test_find_single_pin_nets_empty(self):
        """Find single pin nets in empty netlist."""
        netlist = Mock(spec=Netlist)
        netlist.nets = []
        result = netlist_cmd.find_single_pin_nets(netlist)
        assert result == []

    def test_find_single_pin_nets_none(self):
        """Find single pin nets when none exist."""
        net = Mock(spec=NetlistNet)
        net.connection_count = 2

        netlist = Mock(spec=Netlist)
        netlist.nets = [net]

        result = netlist_cmd.find_single_pin_nets(netlist)
        assert result == []

    def test_find_single_pin_nets_found(self):
        """Find single pin nets."""
        single_net = Mock(spec=NetlistNet)
        single_net.connection_count = 1

        multi_net = Mock(spec=NetlistNet)
        multi_net.connection_count = 3

        netlist = Mock(spec=Netlist)
        netlist.nets = [single_net, multi_net]

        result = netlist_cmd.find_single_pin_nets(netlist)
        assert len(result) == 1
        assert result[0] == single_net

    def test_find_similar_nets(self):
        """Find nets with similar names."""
        net1 = Mock(spec=NetlistNet)
        net1.name = "VCC"

        net2 = Mock(spec=NetlistNet)
        net2.name = "VCC_3V3"

        net3 = Mock(spec=NetlistNet)
        net3.name = "GND"

        netlist = Mock(spec=Netlist)
        netlist.nets = [net1, net2, net3]

        result = netlist_cmd.find_similar_nets(netlist, "vcc")
        assert "VCC" in result
        assert "VCC_3V3" in result
        assert "GND" not in result


class TestNetlistNetlistClass:
    """Tests for Netlist class helper methods."""

    def test_find_single_pin_nets_method(self):
        """Test Netlist.find_single_pin_nets method."""
        netlist = Netlist()

        # Create nets with different connection counts
        single_net = NetlistNet(code=1, name="Net1", nodes=[NetNode(reference="U1", pin="1")])
        multi_net = NetlistNet(
            code=2,
            name="Net2",
            nodes=[
                NetNode(reference="U1", pin="2"),
                NetNode(reference="R1", pin="1"),
            ],
        )

        netlist.nets = [single_net, multi_net]

        result = netlist.find_single_pin_nets()
        assert len(result) == 1
        assert result[0].name == "Net1"

    def test_find_floating_pins_method(self):
        """Test Netlist.find_floating_pins method."""
        netlist = Netlist()

        # Create nets with different connection counts
        single_net = NetlistNet(code=1, name="Net1", nodes=[NetNode(reference="U1", pin="1")])
        multi_net = NetlistNet(
            code=2,
            name="Net2",
            nodes=[
                NetNode(reference="U1", pin="2"),
                NetNode(reference="R1", pin="1"),
            ],
        )

        netlist.nets = [single_net, multi_net]

        result = netlist.find_floating_pins()
        assert len(result) == 1
        assert result[0] == ("U1", "1", "Net1")


class TestNetlistCmdPrinters:
    """Tests for netlist_cmd output functions."""

    def test_print_analyze_text(self, capsys):
        """Test analyze text output."""
        stats = {
            "source_file": "/path/to/test.kicad_sch",
            "tool": "KiCad 8.0",
            "date": "2024-01-01",
            "sheet_count": 1,
            "component_count": 5,
            "components_by_type": {"R": 2, "C": 3},
            "net_count": 10,
            "power_net_count": 2,
            "signal_net_count": 8,
            "single_pin_net_count": 1,
        }

        netlist = Mock(spec=Netlist)
        netlist_cmd.print_analyze_text(stats, netlist)

        captured = capsys.readouterr()
        assert "NETLIST ANALYSIS" in captured.out
        assert "Components: 5" in captured.out
        assert "Nets: 10" in captured.out
        assert "R: 2" in captured.out
        assert "C: 3" in captured.out
        assert "Single-pin nets: 1" in captured.out

    def test_print_list_table_empty(self, capsys):
        """Test list table output for empty netlist."""
        netlist = Mock(spec=Netlist)
        netlist.power_nets = []

        netlist_cmd.print_list_table([], netlist)

        captured = capsys.readouterr()
        assert "No nets found" in captured.out

    def test_print_list_table(self, capsys):
        """Test list table output."""
        node1 = Mock(spec=NetNode)
        node1.reference = "U1"
        node1.pin = "1"

        node2 = Mock(spec=NetNode)
        node2.reference = "R1"
        node2.pin = "2"

        net = Mock(spec=NetlistNet)
        net.name = "VCC"
        net.connection_count = 2
        net.nodes = [node1, node2]

        power_net = Mock(spec=NetlistNet)
        power_net.name = "VCC"

        netlist = Mock(spec=Netlist)
        netlist.power_nets = [power_net]

        netlist_cmd.print_list_table([net], netlist)

        captured = capsys.readouterr()
        assert "VCC" in captured.out
        assert "power" in captured.out
        assert "U1.1" in captured.out
        assert "Total: 1 nets" in captured.out

    def test_print_show_text(self, capsys):
        """Test show text output."""
        node = Mock(spec=NetNode)
        node.reference = "U1"
        node.pin = "1"
        node.pin_function = "VCC"
        node.pin_type = "power_in"

        net = Mock(spec=NetlistNet)
        net.name = "VCC"
        net.code = 1
        net.connection_count = 1
        net.nodes = [node]

        netlist = Mock(spec=Netlist)
        netlist.power_nets = [net]

        netlist_cmd.print_show_text(net, netlist)

        captured = capsys.readouterr()
        assert "Net: VCC" in captured.out
        assert "U1.1" in captured.out
        assert "(VCC)" in captured.out
        assert "[power_in]" in captured.out

    def test_print_check_text_no_issues(self, capsys):
        """Test check text output with no issues."""
        result = {
            "total_nets": 10,
            "power_nets": 2,
            "single_pin_nets": 0,
            "issues": [],
        }

        power_net = Mock(spec=NetlistNet)
        power_net.name = "VCC"
        power_net.connection_count = 5

        netlist_cmd.print_check_text(result, [power_net])

        captured = capsys.readouterr()
        assert "No connectivity issues found" in captured.out
        assert "VCC (5 connections)" in captured.out

    def test_print_check_text_with_issues(self, capsys):
        """Test check text output with issues."""
        result = {
            "total_nets": 10,
            "power_nets": 2,
            "single_pin_nets": 1,
            "issues": [
                {
                    "severity": "warning",
                    "message": "Net 'Net1' has only 1 connection",
                }
            ],
        }

        netlist_cmd.print_check_text(result, [])

        captured = capsys.readouterr()
        assert "Potential Issues (1)" in captured.out
        assert "WARNING" in captured.out
        assert "Single-pin nets: 1" in captured.out

    def test_print_compare_text(self, capsys):
        """Test compare text output."""
        result = {
            "old_file": "/path/to/old.kicad_sch",
            "new_file": "/path/to/new.kicad_sch",
            "components": {
                "added": ["R3", "C4"],
                "removed": ["R1"],
                "added_count": 2,
                "removed_count": 1,
            },
            "nets": {
                "added": ["Net3"],
                "removed": [],
                "modified": [{"name": "VCC", "old": 5, "new": 7, "diff": "+2"}],
                "added_count": 1,
                "removed_count": 0,
                "modified_count": 1,
            },
        }

        netlist_cmd.print_compare_text(result)

        captured = capsys.readouterr()
        assert "NETLIST COMPARISON" in captured.out
        assert "Added (2): R3, C4" in captured.out
        assert "Removed (1): R1" in captured.out
        assert "VCC: 5 → 7 (+2)" in captured.out


class TestNetlistCmdCommands:
    """Tests for netlist command handlers using mocked data."""

    @patch("kicad_tools.cli.netlist_cmd.export_netlist")
    def test_cmd_analyze_text(self, mock_export, capsys):
        """Test analyze command with text output."""
        # Create mock netlist
        mock_netlist = Mock(spec=Netlist)
        mock_netlist.nets = []
        mock_netlist.components = []
        mock_netlist.power_nets = []
        mock_netlist.summary.return_value = {
            "source_file": "test.kicad_sch",
            "tool": "KiCad",
            "date": "2024-01-01",
            "sheet_count": 1,
            "component_count": 0,
            "components_by_type": {},
            "net_count": 0,
            "power_net_count": 0,
            "signal_net_count": 0,
        }
        mock_export.return_value = mock_netlist

        result = netlist_cmd.cmd_analyze(Path("test.kicad_sch"), "text")

        assert result == 0
        captured = capsys.readouterr()
        assert "NETLIST ANALYSIS" in captured.out

    @patch("kicad_tools.cli.netlist_cmd.export_netlist")
    def test_cmd_analyze_json(self, mock_export, capsys):
        """Test analyze command with JSON output."""
        mock_netlist = Mock(spec=Netlist)
        mock_netlist.nets = []
        mock_netlist.summary.return_value = {
            "component_count": 5,
            "net_count": 10,
        }
        mock_export.return_value = mock_netlist

        result = netlist_cmd.cmd_analyze(Path("test.kicad_sch"), "json")

        assert result == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["component_count"] == 5

    @patch("kicad_tools.cli.netlist_cmd.export_netlist")
    def test_cmd_list_table(self, mock_export, capsys):
        """Test list command with table output."""
        node = Mock(spec=NetNode)
        node.reference = "U1"
        node.pin = "1"

        net = Mock(spec=NetlistNet)
        net.name = "VCC"
        net.connection_count = 1
        net.nodes = [node]

        mock_netlist = Mock(spec=Netlist)
        mock_netlist.nets = [net]
        mock_netlist.power_nets = []
        mock_export.return_value = mock_netlist

        result = netlist_cmd.cmd_list(Path("test.kicad_sch"), "table", "connections")

        assert result == 0
        captured = capsys.readouterr()
        assert "VCC" in captured.out

    @patch("kicad_tools.cli.netlist_cmd.export_netlist")
    def test_cmd_show_found(self, mock_export, capsys):
        """Test show command when net is found."""
        node = Mock(spec=NetNode)
        node.reference = "U1"
        node.pin = "1"
        node.pin_function = ""
        node.pin_type = ""

        net = Mock(spec=NetlistNet)
        net.name = "VCC"
        net.code = 1
        net.connection_count = 1
        net.nodes = [node]

        mock_netlist = Mock(spec=Netlist)
        mock_netlist.get_net.return_value = net
        mock_netlist.power_nets = []
        mock_export.return_value = mock_netlist

        result = netlist_cmd.cmd_show(Path("test.kicad_sch"), "VCC", "text")

        assert result == 0
        captured = capsys.readouterr()
        assert "Net: VCC" in captured.out

    @patch("kicad_tools.cli.netlist_cmd.export_netlist")
    def test_cmd_show_not_found(self, mock_export, capsys):
        """Test show command when net is not found."""
        net = Mock(spec=NetlistNet)
        net.name = "VCC"

        mock_netlist = Mock(spec=Netlist)
        mock_netlist.get_net.return_value = None
        mock_netlist.nets = [net]
        mock_export.return_value = mock_netlist

        result = netlist_cmd.cmd_show(Path("test.kicad_sch"), "UNKNOWN", "text")

        assert result == 1
        captured = capsys.readouterr()
        assert "not found" in captured.err

    @patch("kicad_tools.cli.netlist_cmd.export_netlist")
    def test_cmd_check_no_issues(self, mock_export, capsys):
        """Test check command with no issues."""
        mock_netlist = Mock(spec=Netlist)
        mock_netlist.nets = []
        mock_netlist.power_nets = []
        mock_export.return_value = mock_netlist

        result = netlist_cmd.cmd_check(Path("test.kicad_sch"), "text")

        assert result == 0
        captured = capsys.readouterr()
        assert "NETLIST CHECK RESULTS" in captured.out

    @patch("kicad_tools.cli.netlist_cmd.export_netlist")
    def test_cmd_compare(self, mock_export, capsys):
        """Test compare command."""
        comp1 = Mock(spec=NetlistComponent)
        comp1.reference = "R1"

        comp2 = Mock(spec=NetlistComponent)
        comp2.reference = "R2"

        net1 = Mock(spec=NetlistNet)
        net1.name = "VCC"
        net1.connection_count = 5

        net2 = Mock(spec=NetlistNet)
        net2.name = "VCC"
        net2.connection_count = 7

        old_netlist = Mock(spec=Netlist)
        old_netlist.components = [comp1]
        old_netlist.nets = [net1]

        new_netlist = Mock(spec=Netlist)
        new_netlist.components = [comp2]
        new_netlist.nets = [net2]

        mock_export.side_effect = [old_netlist, new_netlist]

        result = netlist_cmd.cmd_compare(Path("old.kicad_sch"), Path("new.kicad_sch"), "text")

        assert result == 0
        captured = capsys.readouterr()
        assert "NETLIST COMPARISON" in captured.out


class TestNetlistCmdMain:
    """Tests for netlist_cmd main entry point."""

    def test_main_no_command(self, capsys):
        """Test main with no command shows help."""
        result = netlist_cmd.main([])

        assert result == 0
        captured = capsys.readouterr()
        assert "Netlist analysis" in captured.out or "usage" in captured.out.lower()

    @patch("kicad_tools.cli.netlist_cmd.export_netlist")
    def test_main_analyze(self, mock_export, capsys):
        """Test main with analyze command."""
        mock_netlist = Mock(spec=Netlist)
        mock_netlist.nets = []
        mock_netlist.summary.return_value = {
            "sheet_count": 1,
            "component_count": 0,
            "net_count": 0,
            "power_net_count": 0,
            "signal_net_count": 0,
        }
        mock_export.return_value = mock_netlist

        result = netlist_cmd.main(["analyze", "test.kicad_sch"])

        assert result == 0

    def test_main_file_not_found(self, capsys):
        """Test main handles FileNotFoundError."""
        result = netlist_cmd.main(["analyze", "/nonexistent/path.kicad_sch"])

        assert result == 1
        captured = capsys.readouterr()
        assert "Error" in captured.err


class TestExportNetlistStaleCache:
    """Tests for export_netlist stale cache prevention (issue #983)."""

    def test_export_netlist_deletes_existing_file_before_export(self, tmp_path):
        """Test that export_netlist deletes existing netlist to prevent stale data."""
        from kicad_tools.operations.netlist import export_netlist

        # Create a minimal schematic
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(
            """(kicad_sch
              (version 20231120)
              (generator "test")
              (uuid "00000000-0000-0000-0000-000000000001")
              (paper "A4")
            )"""
        )

        # Create a stale netlist file with old content
        netlist_file = tmp_path / "test-netlist.kicad_net"
        stale_content = "(export (version D) (components) (nets))"
        netlist_file.write_text(stale_content)

        # Mock find_kicad_cli so export_netlist proceeds to subprocess.run
        with patch("kicad_tools.operations.netlist.find_kicad_cli", return_value=Path("/usr/bin/kicad-cli")):
            # Mock subprocess to simulate kicad-cli not producing output (crash)
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = Mock(returncode=139, stderr="", stdout="")

                # This should raise because kicad-cli crashed (exit 139), fallback disabled
                with pytest.raises(RuntimeError, match="kicad-cli crashed"):
                    export_netlist(sch_file, fallback=False)

                # The stale file should have been deleted BEFORE running kicad-cli
                # (verified by the fact that we got the crash error, not stale data)

    def test_export_netlist_detects_sigsegv_crash(self, tmp_path):
        """Test that exit code 139 (SIGSEGV) is detected with helpful message when fallback disabled."""
        from kicad_tools.operations.netlist import export_netlist

        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(
            """(kicad_sch
              (version 20231120)
              (generator "test")
              (uuid "00000000-0000-0000-0000-000000000001")
              (paper "A4")
            )"""
        )

        with patch("kicad_tools.operations.netlist.find_kicad_cli", return_value=Path("/usr/bin/kicad-cli")):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = Mock(returncode=139, stderr="", stdout="")

                with pytest.raises(RuntimeError) as exc_info:
                    export_netlist(sch_file, fallback=False)

                error_msg = str(exc_info.value)
                assert "SIGSEGV" in error_msg
                assert "problematic symbol" in error_msg
                assert "kicad" in error_msg.lower()

    def test_export_netlist_detects_other_nonzero_exit_codes(self, tmp_path):
        """Test that other non-zero exit codes raise RuntimeError when fallback disabled."""
        from kicad_tools.operations.netlist import export_netlist

        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(
            """(kicad_sch
              (version 20231120)
              (generator "test")
              (uuid "00000000-0000-0000-0000-000000000001")
              (paper "A4")
            )"""
        )

        with patch("kicad_tools.operations.netlist.find_kicad_cli", return_value=Path("/usr/bin/kicad-cli")):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = Mock(returncode=1, stderr="Some error message", stdout="")

                with pytest.raises(RuntimeError, match="kicad-cli failed"):
                    export_netlist(sch_file, fallback=False)

    def test_export_netlist_succeeds_when_kicad_cli_works(self, tmp_path):
        """Test successful export when kicad-cli works properly."""
        from kicad_tools.operations.netlist import export_netlist

        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(
            """(kicad_sch
              (version 20231120)
              (generator "test")
              (uuid "00000000-0000-0000-0000-000000000001")
              (paper "A4")
            )"""
        )

        netlist_file = tmp_path / "test-netlist.kicad_net"
        valid_netlist = """(export
          (version "E")
          (design (source "test.kicad_sch") (tool "Eeschema"))
          (components)
          (nets)
        )"""

        def create_netlist(*args, **kwargs):
            netlist_file.write_text(valid_netlist)
            return Mock(returncode=0, stderr="", stdout="")

        with patch("kicad_tools.operations.netlist.find_kicad_cli", return_value=Path("/usr/bin/kicad-cli")):
            with patch("subprocess.run", side_effect=create_netlist):
                result = export_netlist(sch_file)

                assert result is not None
                assert result.source_file == "test.kicad_sch"

    def test_export_netlist_removes_stale_before_fresh_export(self, tmp_path):
        """Test stale netlist is removed even when fresh export succeeds."""
        from kicad_tools.operations.netlist import export_netlist

        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(
            """(kicad_sch
              (version 20231120)
              (generator "test")
              (uuid "00000000-0000-0000-0000-000000000001")
              (paper "A4")
            )"""
        )

        netlist_file = tmp_path / "test-netlist.kicad_net"
        # Create stale netlist with old timestamp marker
        netlist_file.write_text("(export (version D) (design (date OLD)))")

        fresh_netlist = """(export
          (version "E")
          (design (source "test.kicad_sch") (tool "Eeschema") (date "FRESH"))
          (components)
          (nets)
        )"""

        call_count = [0]

        def track_and_create(*args, **kwargs):
            call_count[0] += 1
            # Verify old file was deleted before subprocess runs
            if call_count[0] == 1:
                assert not netlist_file.exists(), "Stale file should be deleted before export"
            netlist_file.write_text(fresh_netlist)
            return Mock(returncode=0, stderr="", stdout="")

        with patch("kicad_tools.operations.netlist.find_kicad_cli", return_value=Path("/usr/bin/kicad-cli")):
            with patch("subprocess.run", side_effect=track_and_create):
                result = export_netlist(sch_file)

                assert result is not None
                assert "FRESH" in result.date or result.tool == "Eeschema"


class TestExportNetlistPythonFallback:
    """Tests for pure Python netlist extraction fallback (issue #988)."""

    def test_fallback_used_when_kicad_cli_crashes(self, tmp_path):
        """Test that Python fallback is used when kicad-cli crashes with SIGSEGV."""
        from kicad_tools.operations.netlist import export_netlist

        # Create a valid schematic with a symbol
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(
            """(kicad_sch
              (version 20231120)
              (generator "test")
              (uuid "00000000-0000-0000-0000-000000000001")
              (paper "A4")
              (lib_symbols
                (symbol "Device:R" (pin_numbers hide) (pin_names (offset 0)) (in_bom yes) (on_board yes)
                  (property "Reference" "R" (at 2.032 0 90))
                  (property "Value" "R" (at 0 0 90))
                  (property "Footprint" "" (at -1.778 0 90))
                  (symbol "R_0_1"
                    (rectangle (start -1.016 -2.54) (end 1.016 2.54) (stroke (width 0.254)) (fill (type none))))
                  (symbol "R_1_1"
                    (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
                    (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27))))))))
              (symbol (lib_id "Device:R") (at 100 50 0) (unit 1)
                (in_bom yes) (on_board yes)
                (uuid "00000000-0000-0000-0000-000000000010")
                (property "Reference" "R1" (at 101.6 48.26 0))
                (property "Value" "10k" (at 101.6 50.8 0))
                (property "Footprint" "Resistor_SMD:R_0805" (at 0 0 0) (show_name))
                (pin "1" (uuid "00000000-0000-0000-0000-000000000011"))
                (pin "2" (uuid "00000000-0000-0000-0000-000000000012")))
            )"""
        )

        with patch("subprocess.run") as mock_run:
            # Simulate SIGSEGV crash
            mock_run.return_value = Mock(returncode=139, stderr="", stdout="")

            # With fallback=True (default), should use Python extraction
            result = export_netlist(sch_file, fallback=True)

            # Should return a valid Netlist from Python extraction
            assert result is not None
            assert "Python fallback" in result.tool
            assert len(result.components) == 1
            assert result.components[0].reference == "R1"

    def test_fallback_used_when_kicad_cli_not_found(self, tmp_path):
        """Test that Python fallback is used when kicad-cli is not found."""
        from kicad_tools.operations.netlist import export_netlist

        # Create a minimal schematic
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(
            """(kicad_sch
              (version 20231120)
              (generator "test")
              (uuid "00000000-0000-0000-0000-000000000001")
              (paper "A4")
            )"""
        )

        with patch("kicad_tools.operations.netlist.find_kicad_cli", return_value=None):
            # With fallback=True, should use Python extraction instead of raising
            result = export_netlist(sch_file, fallback=True)

            assert result is not None
            assert "Python fallback" in result.tool

    def test_fallback_disabled_raises_on_crash(self, tmp_path):
        """Test that fallback=False still raises RuntimeError on crash."""
        from kicad_tools.operations.netlist import export_netlist

        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(
            """(kicad_sch
              (version 20231120)
              (generator "test")
              (uuid "00000000-0000-0000-0000-000000000001")
              (paper "A4")
            )"""
        )

        with patch("kicad_tools.operations.netlist.find_kicad_cli", return_value=Path("/usr/bin/kicad-cli")):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = Mock(returncode=139, stderr="", stdout="")

                with pytest.raises(RuntimeError, match="SIGSEGV"):
                    export_netlist(sch_file, fallback=False)

    def test_fallback_disabled_raises_on_cli_not_found(self, tmp_path):
        """Test that fallback=False raises FileNotFoundError when kicad-cli not found."""
        from kicad_tools.operations.netlist import export_netlist

        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(
            """(kicad_sch
              (version 20231120)
              (generator "test")
              (uuid "00000000-0000-0000-0000-000000000001")
              (paper "A4")
            )"""
        )

        with patch("kicad_tools.operations.netlist.find_kicad_cli", return_value=None):
            with pytest.raises(FileNotFoundError, match="kicad-cli not found"):
                export_netlist(sch_file, fallback=False)

    def test_fallback_used_when_kicad_cli_fails_with_error(self, tmp_path):
        """Test that Python fallback is used when kicad-cli returns error."""
        from kicad_tools.operations.netlist import export_netlist

        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(
            """(kicad_sch
              (version 20231120)
              (generator "test")
              (uuid "00000000-0000-0000-0000-000000000001")
              (paper "A4")
            )"""
        )

        with patch("subprocess.run") as mock_run:
            # Simulate generic error
            mock_run.return_value = Mock(returncode=1, stderr="Some error", stdout="")

            result = export_netlist(sch_file, fallback=True)

            assert result is not None
            assert "Python fallback" in result.tool


class TestBuildNetlistFromSchematic:
    """Tests for build_netlist_from_schematic() function."""

    def test_build_netlist_from_simple_schematic(self, tmp_path):
        """Test building netlist from a simple schematic."""
        from kicad_tools.operations.netlist import build_netlist_from_schematic

        # Create a schematic with a resistor
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(
            """(kicad_sch
              (version 20231120)
              (generator "test")
              (uuid "00000000-0000-0000-0000-000000000001")
              (paper "A4")
              (lib_symbols
                (symbol "Device:R" (pin_numbers hide) (pin_names (offset 0)) (in_bom yes) (on_board yes)
                  (property "Reference" "R" (at 2.032 0 90))
                  (property "Value" "R" (at 0 0 90))
                  (property "Footprint" "" (at -1.778 0 90))
                  (symbol "R_0_1"
                    (rectangle (start -1.016 -2.54) (end 1.016 2.54) (stroke (width 0.254)) (fill (type none))))
                  (symbol "R_1_1"
                    (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
                    (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27))))))))
              (symbol (lib_id "Device:R") (at 100 50 0) (unit 1)
                (in_bom yes) (on_board yes)
                (uuid "00000000-0000-0000-0000-000000000010")
                (property "Reference" "R1" (at 101.6 48.26 0))
                (property "Value" "10k" (at 101.6 50.8 0))
                (property "Footprint" "Resistor_SMD:R_0805" (at 0 0 0) (show_name))
                (pin "1" (uuid "00000000-0000-0000-0000-000000000011"))
                (pin "2" (uuid "00000000-0000-0000-0000-000000000012")))
            )"""
        )

        result = build_netlist_from_schematic(sch_file)

        assert result is not None
        assert str(sch_file) in result.source_file
        assert "Python fallback" in result.tool
        assert len(result.components) == 1
        assert result.components[0].reference == "R1"
        assert result.components[0].value == "10k"
        assert result.components[0].lib_id == "Device:R"

    def test_build_netlist_from_schematic_not_found(self, tmp_path):
        """Test that FileNotFoundError is raised for missing schematic."""
        from kicad_tools.operations.netlist import build_netlist_from_schematic

        sch_file = tmp_path / "nonexistent.kicad_sch"

        with pytest.raises(FileNotFoundError, match="Schematic not found"):
            build_netlist_from_schematic(sch_file)

    def test_build_netlist_extracts_connectivity(self, tmp_path):
        """Test that connectivity is extracted correctly."""
        from kicad_tools.operations.netlist import build_netlist_from_schematic

        # Create a schematic with two resistors connected by a labeled wire
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(
            """(kicad_sch
              (version 20231120)
              (generator "test")
              (uuid "00000000-0000-0000-0000-000000000001")
              (paper "A4")
              (lib_symbols
                (symbol "Device:R" (pin_numbers hide) (pin_names (offset 0)) (in_bom yes) (on_board yes)
                  (property "Reference" "R" (at 2.032 0 90))
                  (property "Value" "R" (at 0 0 90))
                  (symbol "R_1_1"
                    (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
                    (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27))))))))
              (symbol (lib_id "Device:R") (at 100 50 0) (unit 1)
                (in_bom yes) (on_board yes)
                (uuid "00000000-0000-0000-0000-000000000010")
                (property "Reference" "R1" (at 101.6 48.26 0))
                (property "Value" "10k" (at 101.6 50.8 0))
                (pin "1" (uuid "00000000-0000-0000-0000-000000000011"))
                (pin "2" (uuid "00000000-0000-0000-0000-000000000012")))
              (symbol (lib_id "Device:R") (at 100 70 0) (unit 1)
                (in_bom yes) (on_board yes)
                (uuid "00000000-0000-0000-0000-000000000020")
                (property "Reference" "R2" (at 101.6 68.26 0))
                (property "Value" "20k" (at 101.6 70.8 0))
                (pin "1" (uuid "00000000-0000-0000-0000-000000000021"))
                (pin "2" (uuid "00000000-0000-0000-0000-000000000022")))
              (wire (pts (xy 100 53.81) (xy 100 66.19)))
              (label "NODE_A" (at 100 60 0) (effects (font (size 1.27 1.27))))
            )"""
        )

        result = build_netlist_from_schematic(sch_file)

        # Should have 2 components
        assert len(result.components) == 2
        refs = {c.reference for c in result.components}
        assert "R1" in refs
        assert "R2" in refs

        # Should have nets extracted (at least the NODE_A net connecting R1.2 to R2.1)
        net_names = {n.name for n in result.nets}
        assert "NODE_A" in net_names

        # Check the NODE_A net has the correct pins
        # Note: With the schematic layout, wire connects R1 pin 1 (at y=53.81) to R2 pin 2 (at y=66.19)
        node_a_net = next(n for n in result.nets if n.name == "NODE_A")
        pin_refs = {(node.reference, node.pin) for node in node_a_net.nodes}
        assert ("R1", "1") in pin_refs
        assert ("R2", "2") in pin_refs

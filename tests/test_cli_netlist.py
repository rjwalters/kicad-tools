"""Tests for netlist CLI commands."""

import json
from pathlib import Path
from unittest.mock import Mock, patch

from kicad_tools.cli import netlist_cmd
from kicad_tools.operations.netlist import Netlist, NetlistComponent, NetlistNet, NetNode


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

"""Tests for KiCad 8/9 net format compatibility in DRC modules."""

from __future__ import annotations

from kicad_tools.drc.net_compat import resolve_net_atom


class TestResolveNetAtom:
    """Tests for the resolve_net_atom helper function."""

    def setup_method(self):
        self.nets = {0: "", 1: "GND", 5: "VCC", 10: "+3V3"}
        self.net_names = {"": 0, "GND": 1, "VCC": 5, "+3V3": 10}

    def test_integer_format_kicad8(self):
        """KiCad 8 format: (net 5) -> atom is '5'."""
        net_num, net_name = resolve_net_atom("5", self.nets, self.net_names)
        assert net_num == 5
        assert net_name == "VCC"

    def test_name_format_kicad9(self):
        """KiCad 9 format: (net "GND") -> atom is 'GND'."""
        net_num, net_name = resolve_net_atom("GND", self.nets, self.net_names)
        assert net_num == 1
        assert net_name == "GND"

    def test_name_format_plus_prefix(self):
        """KiCad 9 format with special chars: (net "+3V3")."""
        net_num, net_name = resolve_net_atom("+3V3", self.nets, self.net_names)
        assert net_num == 10
        assert net_name == "+3V3"

    def test_none_atom(self):
        """None atom returns (0, '')."""
        net_num, net_name = resolve_net_atom(None, self.nets, self.net_names)
        assert net_num == 0
        assert net_name == ""

    def test_empty_string(self):
        """Empty string returns (0, '')."""
        net_num, net_name = resolve_net_atom("", self.nets, self.net_names)
        assert net_num == 0
        assert net_name == ""

    def test_unknown_name(self):
        """Unknown name string returns (0, name)."""
        net_num, net_name = resolve_net_atom("UNKNOWN", self.nets, self.net_names)
        assert net_num == 0
        assert net_name == "UNKNOWN"

    def test_unknown_integer(self):
        """Unknown integer returns (int, '')."""
        net_num, net_name = resolve_net_atom("99", self.nets, self.net_names)
        assert net_num == 99
        assert net_name == ""

    def test_zero_integer(self):
        """Zero net ID returns (0, '') -- unconnected net."""
        net_num, net_name = resolve_net_atom("0", self.nets, self.net_names)
        assert net_num == 0
        assert net_name == ""

    def test_no_dicts(self):
        """Works with None dicts (graceful fallback)."""
        net_num, net_name = resolve_net_atom("5", None, None)
        assert net_num == 5
        assert net_name == ""

        net_num, net_name = resolve_net_atom("GND", None, None)
        assert net_num == 0
        assert net_name == "GND"

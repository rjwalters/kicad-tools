"""Tests for bus routing module."""

from kicad_tools.router.bus import (
    BusGroup,
    BusRoutingConfig,
    BusRoutingMode,
    BusSignal,
    analyze_buses,
    detect_bus_signals,
    group_buses,
    parse_bus_signal,
)


class TestParseBusSignal:
    """Tests for bus signal name parsing."""

    def test_bracket_notation(self):
        """Test parsing bracket notation like DATA[7]."""
        result = parse_bus_signal("DATA[7]")
        assert result is not None
        bus_name, index, notation = result
        assert bus_name == "DATA"
        assert index == 7
        assert notation == "bracket"

    def test_bracket_notation_multi_digit(self):
        """Test parsing bracket notation with multi-digit index."""
        result = parse_bus_signal("ADDR[15]")
        assert result is not None
        bus_name, index, notation = result
        assert bus_name == "ADDR"
        assert index == 15
        assert notation == "bracket"

    def test_bracket_notation_with_underscore(self):
        """Test parsing bracket notation with underscore in name."""
        result = parse_bus_signal("DATA_BUS[3]")
        assert result is not None
        bus_name, index, notation = result
        assert bus_name == "DATA_BUS"
        assert index == 3
        assert notation == "bracket"

    def test_underscore_notation(self):
        """Test parsing underscore notation like DATA_7."""
        result = parse_bus_signal("DATA_7")
        assert result is not None
        bus_name, index, notation = result
        assert bus_name == "DATA"
        assert index == 7
        assert notation == "underscore"

    def test_underscore_notation_multi_digit(self):
        """Test parsing underscore notation with multi-digit index."""
        result = parse_bus_signal("ADDR_15")
        assert result is not None
        bus_name, index, notation = result
        assert bus_name == "ADDR"
        assert index == 15
        assert notation == "underscore"

    def test_numeric_suffix_notation(self):
        """Test parsing numeric suffix like DATA7."""
        result = parse_bus_signal("DATA7")
        assert result is not None
        bus_name, index, notation = result
        assert bus_name == "DATA"
        assert index == 7
        assert notation == "numeric"

    def test_numeric_suffix_multi_digit(self):
        """Test parsing numeric suffix with multi-digit index."""
        result = parse_bus_signal("ADDR15")
        assert result is not None
        bus_name, index, notation = result
        assert bus_name == "ADDR"
        assert index == 15
        assert notation == "numeric"

    def test_non_bus_signal(self):
        """Test that non-bus signals return None."""
        assert parse_bus_signal("VCC") is None
        assert parse_bus_signal("GND") is None
        assert parse_bus_signal("CLK") is None
        assert parse_bus_signal("RESET") is None

    def test_single_digit_number(self):
        """Test single character names don't match numeric pattern."""
        # Single letter followed by number shouldn't match (needs at least 2 chars before)
        result = parse_bus_signal("D7")
        # This should match underscore pattern: D_7 would be "D" with index 7
        # But D7 doesn't have underscore, and numeric pattern needs 2+ letters
        assert result is None  # "D7" doesn't match: needs at least 2 chars before digit

    def test_complex_bus_names(self):
        """Test complex bus names with mixed characters."""
        result = parse_bus_signal("I2C_SDA_0")
        assert result is not None
        assert result[0] == "I2C_SDA"
        assert result[1] == 0

        result = parse_bus_signal("SPI_MOSI[0]")
        assert result is not None
        assert result[0] == "SPI_MOSI"
        assert result[1] == 0


class TestBusSignal:
    """Tests for BusSignal dataclass."""

    def test_bus_signal_creation(self):
        """Test creating a BusSignal."""
        signal = BusSignal(
            net_name="DATA[0]",
            net_id=1,
            bus_name="DATA",
            index=0,
            notation="bracket",
        )
        assert signal.net_name == "DATA[0]"
        assert signal.net_id == 1
        assert signal.bus_name == "DATA"
        assert signal.index == 0
        assert signal.notation == "bracket"

    def test_bus_signal_hash(self):
        """Test BusSignal hashing."""
        signal1 = BusSignal("DATA[0]", 1, "DATA", 0, "bracket")
        signal2 = BusSignal("DATA[0]", 1, "DATA", 0, "bracket")
        assert hash(signal1) == hash(signal2)

    def test_bus_signal_equality(self):
        """Test BusSignal equality."""
        signal1 = BusSignal("DATA[0]", 1, "DATA", 0, "bracket")
        signal2 = BusSignal("DATA[0]", 1, "DATA", 0, "bracket")
        signal3 = BusSignal("DATA[1]", 2, "DATA", 1, "bracket")
        assert signal1 == signal2
        assert signal1 != signal3


class TestBusGroup:
    """Tests for BusGroup dataclass."""

    def test_bus_group_creation(self):
        """Test creating a BusGroup."""
        group = BusGroup(name="DATA")
        assert group.name == "DATA"
        assert group.signals == []
        assert group.width == 0

    def test_bus_group_with_signals(self):
        """Test BusGroup with signals."""
        signals = [
            BusSignal("DATA[0]", 1, "DATA", 0, "bracket"),
            BusSignal("DATA[1]", 2, "DATA", 1, "bracket"),
            BusSignal("DATA[2]", 3, "DATA", 2, "bracket"),
        ]
        group = BusGroup(name="DATA", signals=signals)
        assert group.width == 3
        assert group.min_index == 0
        assert group.max_index == 2

    def test_bus_group_is_complete(self):
        """Test is_complete for contiguous indices."""
        signals = [
            BusSignal("DATA[0]", 1, "DATA", 0, "bracket"),
            BusSignal("DATA[1]", 2, "DATA", 1, "bracket"),
            BusSignal("DATA[2]", 3, "DATA", 2, "bracket"),
        ]
        group = BusGroup(name="DATA", signals=signals)
        assert group.is_complete() is True

    def test_bus_group_is_incomplete(self):
        """Test is_complete for non-contiguous indices."""
        signals = [
            BusSignal("DATA[0]", 1, "DATA", 0, "bracket"),
            BusSignal("DATA[2]", 3, "DATA", 2, "bracket"),  # Missing [1]
        ]
        group = BusGroup(name="DATA", signals=signals)
        assert group.is_complete() is False

    def test_bus_group_get_net_ids(self):
        """Test get_net_ids returns IDs in bit order."""
        signals = [
            BusSignal("DATA[2]", 3, "DATA", 2, "bracket"),
            BusSignal("DATA[0]", 1, "DATA", 0, "bracket"),
            BusSignal("DATA[1]", 2, "DATA", 1, "bracket"),
        ]
        group = BusGroup(name="DATA", signals=signals)
        assert group.get_net_ids() == [1, 2, 3]  # Sorted by index

    def test_bus_group_str(self):
        """Test BusGroup string representation."""
        signals = [
            BusSignal("DATA[0]", 1, "DATA", 0, "bracket"),
            BusSignal("DATA[7]", 8, "DATA", 7, "bracket"),
        ]
        group = BusGroup(name="DATA", signals=signals)
        assert str(group) == "DATA[7:0]"


class TestDetectBusSignals:
    """Tests for bus signal detection."""

    def test_detect_single_bus(self):
        """Test detecting a single bus."""
        net_names = {
            1: "DATA[0]",
            2: "DATA[1]",
            3: "DATA[2]",
            4: "DATA[3]",
        }
        signals = detect_bus_signals(net_names)
        assert len(signals) == 4
        assert all(s.bus_name == "DATA" for s in signals)

    def test_detect_multiple_buses(self):
        """Test detecting multiple buses."""
        net_names = {
            1: "DATA[0]",
            2: "DATA[1]",
            3: "ADDR[0]",
            4: "ADDR[1]",
        }
        signals = detect_bus_signals(net_names)
        assert len(signals) == 4
        data_signals = [s for s in signals if s.bus_name == "DATA"]
        addr_signals = [s for s in signals if s.bus_name == "ADDR"]
        assert len(data_signals) == 2
        assert len(addr_signals) == 2

    def test_detect_mixed_notation(self):
        """Test detecting buses with different notation styles."""
        net_names = {
            1: "DATA[0]",  # bracket
            2: "DATA[1]",
            3: "CTRL_0",  # underscore
            4: "CTRL_1",
        }
        signals = detect_bus_signals(net_names)
        assert len(signals) == 4

    def test_min_bus_width_filter(self):
        """Test that signals below min_bus_width are excluded."""
        net_names = {
            1: "DATA[0]",  # Only 1 signal - below threshold
            2: "ADDR[0]",
            3: "ADDR[1]",
            4: "ADDR[2]",
        }
        signals = detect_bus_signals(net_names, min_bus_width=2)
        # DATA should be excluded (only 1 signal)
        assert len(signals) == 3
        assert all(s.bus_name == "ADDR" for s in signals)

    def test_detect_with_non_bus_signals(self):
        """Test detection with mixed bus and non-bus signals."""
        net_names = {
            1: "DATA[0]",
            2: "DATA[1]",
            3: "VCC",
            4: "GND",
            5: "CLK",
        }
        signals = detect_bus_signals(net_names)
        assert len(signals) == 2
        assert all(s.bus_name == "DATA" for s in signals)


class TestGroupBuses:
    """Tests for bus grouping."""

    def test_group_single_bus(self):
        """Test grouping signals into a single bus."""
        signals = [
            BusSignal("DATA[0]", 1, "DATA", 0, "bracket"),
            BusSignal("DATA[1]", 2, "DATA", 1, "bracket"),
            BusSignal("DATA[2]", 3, "DATA", 2, "bracket"),
        ]
        groups = group_buses(signals)
        assert len(groups) == 1
        assert groups[0].name == "DATA"
        assert groups[0].width == 3

    def test_group_multiple_buses(self):
        """Test grouping signals into multiple buses."""
        signals = [
            BusSignal("DATA[0]", 1, "DATA", 0, "bracket"),
            BusSignal("DATA[1]", 2, "DATA", 1, "bracket"),
            BusSignal("ADDR[0]", 3, "ADDR", 0, "bracket"),
            BusSignal("ADDR[1]", 4, "ADDR", 1, "bracket"),
        ]
        groups = group_buses(signals)
        assert len(groups) == 2
        group_names = {g.name for g in groups}
        assert group_names == {"DATA", "ADDR"}

    def test_group_sorted_by_index(self):
        """Test that signals in groups are sorted by index."""
        signals = [
            BusSignal("DATA[3]", 4, "DATA", 3, "bracket"),
            BusSignal("DATA[1]", 2, "DATA", 1, "bracket"),
            BusSignal("DATA[0]", 1, "DATA", 0, "bracket"),
            BusSignal("DATA[2]", 3, "DATA", 2, "bracket"),
        ]
        groups = group_buses(signals)
        assert len(groups) == 1
        indices = [s.index for s in groups[0].signals]
        assert indices == [0, 1, 2, 3]


class TestBusRoutingConfig:
    """Tests for BusRoutingConfig."""

    def test_default_config(self):
        """Test default configuration values."""
        config = BusRoutingConfig()
        assert config.enabled is False
        assert config.mode == BusRoutingMode.PARALLEL
        assert config.spacing is None
        assert config.min_bus_width == 2
        assert config.maintain_order is True

    def test_enabled_config(self):
        """Test enabled configuration."""
        config = BusRoutingConfig(enabled=True, mode=BusRoutingMode.STACKED)
        assert config.enabled is True
        assert config.mode == BusRoutingMode.STACKED

    def test_get_spacing_auto(self):
        """Test auto spacing calculation."""
        config = BusRoutingConfig()
        spacing = config.get_spacing(trace_width=0.2, clearance=0.15)
        assert spacing == 0.35  # 0.2 + 0.15

    def test_get_spacing_custom(self):
        """Test custom spacing."""
        config = BusRoutingConfig(spacing=0.5)
        spacing = config.get_spacing(trace_width=0.2, clearance=0.15)
        assert spacing == 0.5


class TestAnalyzeBuses:
    """Tests for bus analysis function."""

    def test_analyze_buses(self):
        """Test bus analysis output."""
        net_names = {
            1: "DATA[0]",
            2: "DATA[1]",
            3: "DATA[2]",
            4: "VCC",
            5: "GND",
        }
        analysis = analyze_buses(net_names)

        assert analysis["total_signals"] == 3
        assert analysis["total_groups"] == 1
        assert len(analysis["groups"]) == 1
        assert analysis["groups"][0]["name"] == "DATA[2:0]"
        assert analysis["groups"][0]["width"] == 3
        assert analysis["groups"][0]["complete"] is True
        assert set(analysis["non_bus_nets"]) == {"VCC", "GND"}

    def test_analyze_empty(self):
        """Test bus analysis with no buses."""
        net_names = {1: "VCC", 2: "GND"}
        analysis = analyze_buses(net_names)

        assert analysis["total_signals"] == 0
        assert analysis["total_groups"] == 0
        assert len(analysis["groups"]) == 0

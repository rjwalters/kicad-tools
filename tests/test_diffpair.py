"""Tests for differential pair routing module."""

from kicad_tools.router.diffpair import (
    DifferentialPair,
    DifferentialPairConfig,
    DifferentialPairRules,
    DifferentialPairType,
    DifferentialSignal,
    LengthMismatchWarning,
    analyze_differential_pairs,
    detect_differential_pairs,
    detect_differential_signals,
    group_differential_pairs,
    parse_differential_signal,
)


class TestParseDifferentialSignal:
    """Tests for differential signal name parsing."""

    def test_plus_minus_notation_positive(self):
        """Test parsing plus notation like USB_D+."""
        result = parse_differential_signal("USB_D+")
        assert result is not None
        base_name, polarity, notation = result
        assert base_name == "USB_D"
        assert polarity == "P"
        assert notation == "plus_minus"

    def test_plus_minus_notation_negative(self):
        """Test parsing minus notation like USB_D-."""
        result = parse_differential_signal("USB_D-")
        assert result is not None
        base_name, polarity, notation = result
        assert base_name == "USB_D"
        assert polarity == "N"
        assert notation == "plus_minus"

    def test_plus_minus_ethernet(self):
        """Test parsing Ethernet differential pairs."""
        result = parse_differential_signal("ETH_TX+")
        assert result is not None
        assert result[0] == "ETH_TX"
        assert result[1] == "P"

        result = parse_differential_signal("ETH_TX-")
        assert result is not None
        assert result[0] == "ETH_TX"
        assert result[1] == "N"

    def test_pn_suffix_notation(self):
        """Test parsing P/N suffix like HDMI_D0_P."""
        result = parse_differential_signal("HDMI_D0_P")
        assert result is not None
        base_name, polarity, notation = result
        assert base_name == "HDMI_D0"
        assert polarity == "P"
        assert notation == "pn_suffix"

        result = parse_differential_signal("HDMI_D0_N")
        assert result is not None
        base_name, polarity, notation = result
        assert base_name == "HDMI_D0"
        assert polarity == "N"
        assert notation == "pn_suffix"

    def test_pn_suffix_lowercase(self):
        """Test parsing lowercase P/N suffix."""
        result = parse_differential_signal("usb3_tx_p")
        assert result is not None
        assert result[0] == "usb3_tx"
        assert result[1] == "P"

        result = parse_differential_signal("usb3_tx_n")
        assert result is not None
        assert result[0] == "usb3_tx"
        assert result[1] == "N"

    def test_dp_dn_suffix_notation(self):
        """Test parsing DP/DN suffix like CLK_DP."""
        result = parse_differential_signal("CLK_DP")
        assert result is not None
        base_name, polarity, notation = result
        assert base_name == "CLK"
        assert polarity == "P"
        assert notation == "pn_suffix"

        result = parse_differential_signal("CLK_DN")
        assert result is not None
        assert result[1] == "N"

    def test_pos_neg_notation(self):
        """Test parsing POS/NEG suffix like CLK_POS."""
        result = parse_differential_signal("CLK_POS")
        assert result is not None
        base_name, polarity, notation = result
        assert base_name == "CLK"
        assert polarity == "P"
        assert notation == "pos_neg"

        result = parse_differential_signal("CLK_NEG")
        assert result is not None
        base_name, polarity, notation = result
        assert base_name == "CLK"
        assert polarity == "N"
        assert notation == "pos_neg"

    def test_non_differential_signal(self):
        """Test that non-differential signals return None."""
        assert parse_differential_signal("VCC") is None
        assert parse_differential_signal("GND") is None
        assert parse_differential_signal("CLK") is None
        assert parse_differential_signal("DATA[0]") is None
        assert parse_differential_signal("RESET") is None

    def test_complex_names(self):
        """Test complex signal names."""
        result = parse_differential_signal("USB3_SS_TX_P")
        assert result is not None
        assert result[0] == "USB3_SS_TX"
        assert result[1] == "P"

        result = parse_differential_signal("HDMI_TMDS_D2+")
        assert result is not None
        assert result[0] == "HDMI_TMDS_D2"
        assert result[1] == "P"


class TestDifferentialSignal:
    """Tests for DifferentialSignal dataclass."""

    def test_signal_creation(self):
        """Test creating a DifferentialSignal."""
        signal = DifferentialSignal(
            net_name="USB_D+",
            net_id=1,
            base_name="USB_D",
            polarity="P",
            notation="plus_minus",
        )
        assert signal.net_name == "USB_D+"
        assert signal.net_id == 1
        assert signal.base_name == "USB_D"
        assert signal.polarity == "P"
        assert signal.notation == "plus_minus"

    def test_signal_hash(self):
        """Test DifferentialSignal hashing."""
        signal1 = DifferentialSignal("USB_D+", 1, "USB_D", "P", "plus_minus")
        signal2 = DifferentialSignal("USB_D+", 1, "USB_D", "P", "plus_minus")
        assert hash(signal1) == hash(signal2)

    def test_signal_equality(self):
        """Test DifferentialSignal equality."""
        signal1 = DifferentialSignal("USB_D+", 1, "USB_D", "P", "plus_minus")
        signal2 = DifferentialSignal("USB_D+", 1, "USB_D", "P", "plus_minus")
        signal3 = DifferentialSignal("USB_D-", 2, "USB_D", "N", "plus_minus")
        assert signal1 == signal2
        assert signal1 != signal3


class TestDifferentialPairRules:
    """Tests for DifferentialPairRules."""

    def test_usb2_rules(self):
        """Test USB 2.0 differential pair rules."""
        rules = DifferentialPairRules.for_type(DifferentialPairType.USB2)
        assert rules.spacing == 0.2
        assert rules.max_length_delta == 2.5
        assert rules.impedance == 90.0

    def test_usb3_rules(self):
        """Test USB 3.0 differential pair rules."""
        rules = DifferentialPairRules.for_type(DifferentialPairType.USB3)
        assert rules.spacing == 0.15
        assert rules.max_length_delta == 0.5
        assert rules.impedance == 90.0

    def test_ethernet_rules(self):
        """Test Ethernet differential pair rules."""
        rules = DifferentialPairRules.for_type(DifferentialPairType.ETHERNET)
        assert rules.spacing == 0.2
        assert rules.max_length_delta == 2.0
        assert rules.impedance == 100.0

    def test_hdmi_rules(self):
        """Test HDMI differential pair rules."""
        rules = DifferentialPairRules.for_type(DifferentialPairType.HDMI)
        assert rules.spacing == 0.15
        assert rules.max_length_delta == 0.5
        assert rules.impedance == 100.0

    def test_lvds_rules(self):
        """Test LVDS differential pair rules."""
        rules = DifferentialPairRules.for_type(DifferentialPairType.LVDS)
        assert rules.spacing == 0.15
        assert rules.max_length_delta == 0.5
        assert rules.impedance == 100.0


class TestDifferentialPair:
    """Tests for DifferentialPair dataclass."""

    def test_pair_creation(self):
        """Test creating a DifferentialPair."""
        positive = DifferentialSignal("USB_D+", 1, "USB_D", "P", "plus_minus")
        negative = DifferentialSignal("USB_D-", 2, "USB_D", "N", "plus_minus")
        pair = DifferentialPair(
            name="USB_D",
            positive=positive,
            negative=negative,
            pair_type=DifferentialPairType.USB2,
        )
        assert pair.name == "USB_D"
        assert pair.positive == positive
        assert pair.negative == negative
        assert pair.pair_type == DifferentialPairType.USB2
        assert pair.rules is not None

    def test_pair_length_delta(self):
        """Test length delta calculation."""
        positive = DifferentialSignal("USB_D+", 1, "USB_D", "P", "plus_minus")
        negative = DifferentialSignal("USB_D-", 2, "USB_D", "N", "plus_minus")
        pair = DifferentialPair(
            name="USB_D",
            positive=positive,
            negative=negative,
        )
        pair.routed_length_p = 10.0
        pair.routed_length_n = 10.5
        assert pair.length_delta == 0.5

    def test_pair_is_length_matched(self):
        """Test length matching check."""
        positive = DifferentialSignal("USB_D+", 1, "USB_D", "P", "plus_minus")
        negative = DifferentialSignal("USB_D-", 2, "USB_D", "N", "plus_minus")
        pair = DifferentialPair(
            name="USB_D",
            positive=positive,
            negative=negative,
            pair_type=DifferentialPairType.USB2,
        )
        # USB2 max delta is 2.5mm
        pair.routed_length_p = 10.0
        pair.routed_length_n = 12.0
        assert pair.is_length_matched  # 2.0 < 2.5

        pair.routed_length_n = 13.0
        assert not pair.is_length_matched  # 3.0 > 2.5

    def test_pair_get_net_ids(self):
        """Test getting net IDs from pair."""
        positive = DifferentialSignal("USB_D+", 5, "USB_D", "P", "plus_minus")
        negative = DifferentialSignal("USB_D-", 6, "USB_D", "N", "plus_minus")
        pair = DifferentialPair(
            name="USB_D",
            positive=positive,
            negative=negative,
        )
        assert pair.get_net_ids() == (5, 6)

    def test_pair_str(self):
        """Test string representation."""
        positive = DifferentialSignal("USB_D+", 1, "USB_D", "P", "plus_minus")
        negative = DifferentialSignal("USB_D-", 2, "USB_D", "N", "plus_minus")
        pair = DifferentialPair(name="USB_D", positive=positive, negative=negative)
        assert str(pair) == "USB_D+/-"

        positive = DifferentialSignal("HDMI_D0_P", 1, "HDMI_D0", "P", "pn_suffix")
        negative = DifferentialSignal("HDMI_D0_N", 2, "HDMI_D0", "N", "pn_suffix")
        pair = DifferentialPair(name="HDMI_D0", positive=positive, negative=negative)
        assert str(pair) == "HDMI_D0_P/N"


class TestDetectDifferentialSignals:
    """Tests for detect_differential_signals function."""

    def test_detect_usb_signals(self):
        """Test detecting USB differential signals."""
        net_names = {
            1: "USB_D+",
            2: "USB_D-",
            3: "VCC",
            4: "GND",
        }
        signals = detect_differential_signals(net_names)
        assert len(signals) == 2

        p_signal = next(s for s in signals if s.polarity == "P")
        assert p_signal.net_name == "USB_D+"
        assert p_signal.base_name == "USB_D"

        n_signal = next(s for s in signals if s.polarity == "N")
        assert n_signal.net_name == "USB_D-"
        assert n_signal.base_name == "USB_D"

    def test_detect_multiple_pairs(self):
        """Test detecting multiple differential pairs."""
        net_names = {
            1: "USB_D+",
            2: "USB_D-",
            3: "ETH_TX+",
            4: "ETH_TX-",
            5: "ETH_RX+",
            6: "ETH_RX-",
        }
        signals = detect_differential_signals(net_names)
        assert len(signals) == 6


class TestGroupDifferentialPairs:
    """Tests for group_differential_pairs function."""

    def test_group_complete_pair(self):
        """Test grouping a complete differential pair."""
        signals = [
            DifferentialSignal("USB_D+", 1, "USB_D", "P", "plus_minus"),
            DifferentialSignal("USB_D-", 2, "USB_D", "N", "plus_minus"),
        ]
        pairs = group_differential_pairs(signals)
        assert len(pairs) == 1
        assert pairs[0].name == "USB_D"
        assert pairs[0].positive.net_id == 1
        assert pairs[0].negative.net_id == 2

    def test_group_multiple_pairs(self):
        """Test grouping multiple pairs."""
        signals = [
            DifferentialSignal("USB_D+", 1, "USB_D", "P", "plus_minus"),
            DifferentialSignal("USB_D-", 2, "USB_D", "N", "plus_minus"),
            DifferentialSignal("ETH_TX+", 3, "ETH_TX", "P", "plus_minus"),
            DifferentialSignal("ETH_TX-", 4, "ETH_TX", "N", "plus_minus"),
        ]
        pairs = group_differential_pairs(signals)
        assert len(pairs) == 2
        pair_names = {p.name for p in pairs}
        assert pair_names == {"USB_D", "ETH_TX"}

    def test_group_incomplete_pair_ignored(self):
        """Test that incomplete pairs are not grouped."""
        signals = [
            DifferentialSignal("USB_D+", 1, "USB_D", "P", "plus_minus"),
            # Missing USB_D-
            DifferentialSignal("ETH_TX+", 3, "ETH_TX", "P", "plus_minus"),
            DifferentialSignal("ETH_TX-", 4, "ETH_TX", "N", "plus_minus"),
        ]
        pairs = group_differential_pairs(signals)
        assert len(pairs) == 1
        assert pairs[0].name == "ETH_TX"

    def test_pair_type_detection(self):
        """Test automatic pair type detection."""
        signals = [
            DifferentialSignal("USB_D+", 1, "USB_D", "P", "plus_minus"),
            DifferentialSignal("USB_D-", 2, "USB_D", "N", "plus_minus"),
            DifferentialSignal("USB3_TX_P", 3, "USB3_TX", "P", "pn_suffix"),
            DifferentialSignal("USB3_TX_N", 4, "USB3_TX", "N", "pn_suffix"),
            DifferentialSignal("ETH_RX+", 5, "ETH_RX", "P", "plus_minus"),
            DifferentialSignal("ETH_RX-", 6, "ETH_RX", "N", "plus_minus"),
            DifferentialSignal("HDMI_D0_P", 7, "HDMI_D0", "P", "pn_suffix"),
            DifferentialSignal("HDMI_D0_N", 8, "HDMI_D0", "N", "pn_suffix"),
        ]
        pairs = group_differential_pairs(signals)

        usb2_pair = next(p for p in pairs if p.name == "USB_D")
        assert usb2_pair.pair_type == DifferentialPairType.USB2

        usb3_pair = next(p for p in pairs if p.name == "USB3_TX")
        assert usb3_pair.pair_type == DifferentialPairType.USB3

        eth_pair = next(p for p in pairs if p.name == "ETH_RX")
        assert eth_pair.pair_type == DifferentialPairType.ETHERNET

        hdmi_pair = next(p for p in pairs if p.name == "HDMI_D0")
        assert hdmi_pair.pair_type == DifferentialPairType.HDMI


class TestDetectDifferentialPairs:
    """Tests for detect_differential_pairs convenience function."""

    def test_detect_pairs(self):
        """Test detecting and grouping differential pairs."""
        net_names = {
            1: "USB_D+",
            2: "USB_D-",
            3: "ETH_TX+",
            4: "ETH_TX-",
            5: "VCC",
            6: "GND",
        }
        pairs = detect_differential_pairs(net_names)
        assert len(pairs) == 2
        pair_names = {p.name for p in pairs}
        assert pair_names == {"USB_D", "ETH_TX"}


class TestDifferentialPairConfig:
    """Tests for DifferentialPairConfig."""

    def test_default_config(self):
        """Test default configuration."""
        config = DifferentialPairConfig()
        assert config.enabled is False
        assert config.auto_detect is True
        assert config.spacing is None
        assert config.max_length_delta is None
        assert config.add_serpentines is True

    def test_enabled_config(self):
        """Test enabled configuration."""
        config = DifferentialPairConfig(enabled=True)
        assert config.enabled is True

    def test_get_rules_with_override(self):
        """Test get_rules with config overrides."""
        config = DifferentialPairConfig(
            enabled=True,
            spacing=0.25,
            max_length_delta=1.0,
        )
        rules = config.get_rules(DifferentialPairType.USB2)
        assert rules.spacing == 0.25  # Overridden
        assert rules.max_length_delta == 1.0  # Overridden
        assert rules.impedance == 90.0  # From base USB2 rules

    def test_get_rules_without_override(self):
        """Test get_rules without config overrides."""
        config = DifferentialPairConfig(enabled=True)
        rules = config.get_rules(DifferentialPairType.ETHERNET)
        assert rules.spacing == 0.2  # From base Ethernet rules
        assert rules.max_length_delta == 2.0  # From base Ethernet rules


class TestLengthMismatchWarning:
    """Tests for LengthMismatchWarning."""

    def test_warning_str(self):
        """Test warning string representation."""
        positive = DifferentialSignal("USB_D+", 1, "USB_D", "P", "plus_minus")
        negative = DifferentialSignal("USB_D-", 2, "USB_D", "N", "plus_minus")
        pair = DifferentialPair(name="USB_D", positive=positive, negative=negative)
        warning = LengthMismatchWarning(pair=pair, delta=3.5, max_allowed=2.5)
        warning_str = str(warning)
        assert "USB_D" in warning_str
        assert "3.500mm" in warning_str
        assert "2.500mm" in warning_str


class TestAnalyzeDifferentialPairs:
    """Tests for analyze_differential_pairs function."""

    def test_analyze_with_pairs(self):
        """Test analysis with complete pairs."""
        net_names = {
            1: "USB_D+",
            2: "USB_D-",
            3: "ETH_TX+",
            4: "ETH_TX-",
        }
        analysis = analyze_differential_pairs(net_names)
        assert analysis["total_pairs"] == 2
        assert analysis["total_signals"] == 4
        assert analysis["unpaired_signals"] == 0
        assert len(analysis["pairs"]) == 2

    def test_analyze_with_unpaired(self):
        """Test analysis with unpaired signals."""
        net_names = {
            1: "USB_D+",
            2: "USB_D-",
            3: "ORPHAN+",  # No matching ORPHAN-
        }
        analysis = analyze_differential_pairs(net_names)
        assert analysis["total_pairs"] == 1
        assert analysis["total_signals"] == 3
        assert analysis["unpaired_signals"] == 1
        assert len(analysis["unpaired"]) == 1
        assert analysis["unpaired"][0]["net_name"] == "ORPHAN+"

    def test_analyze_empty(self):
        """Test analysis with no differential signals."""
        net_names = {
            1: "VCC",
            2: "GND",
            3: "CLK",
        }
        analysis = analyze_differential_pairs(net_names)
        assert analysis["total_pairs"] == 0
        assert analysis["total_signals"] == 0

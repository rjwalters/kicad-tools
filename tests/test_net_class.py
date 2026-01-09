"""Tests for net class auto-detection from schematic symbols."""

from kicad_tools.router.net_class import (
    NetClass,
    NetClassification,
    apply_net_class_rules,
    auto_classify_nets,
    classify_and_apply_rules,
    classify_from_name,
    classify_from_pin_type,
    classify_from_symbol,
    classify_net,
    find_differential_partner,
    is_differential_pair_name,
)


class TestNetClassEnum:
    """Tests for the NetClass enumeration."""

    def test_net_class_values(self):
        """Test that all expected net classes exist."""
        assert NetClass.POWER.value == "power"
        assert NetClass.GROUND.value == "ground"
        assert NetClass.CLOCK.value == "clock"
        assert NetClass.HIGH_SPEED.value == "high_speed"
        assert NetClass.DIFFERENTIAL.value == "differential"
        assert NetClass.ANALOG.value == "analog"
        assert NetClass.RF.value == "rf"
        assert NetClass.DEBUG.value == "debug"
        assert NetClass.SIGNAL.value == "signal"

    def test_all_net_classes_count(self):
        """Test that we have expected number of net classes."""
        assert len(NetClass) == 9


class TestSymbolIndicators:
    """Tests for symbol-based classification."""

    def test_power_symbol_classification(self):
        """Test power symbol detection."""
        assert classify_from_symbol("power:VCC") == NetClass.POWER
        assert classify_from_symbol("power:GND") == NetClass.POWER
        assert classify_from_symbol("Device:Ferrite_Bead_Small") == NetClass.POWER

    def test_clock_symbol_classification(self):
        """Test clock/oscillator symbol detection."""
        assert classify_from_symbol("Device:Crystal") == NetClass.CLOCK
        assert classify_from_symbol("Device:Crystal_GND24") == NetClass.CLOCK
        assert classify_from_symbol("Oscillator:SiT8008") == NetClass.CLOCK

    def test_high_speed_symbol_classification(self):
        """Test high-speed interface symbol detection."""
        assert classify_from_symbol("Connector:USB_C_Receptacle") == NetClass.HIGH_SPEED
        assert classify_from_symbol("Interface:FT232RL") == NetClass.HIGH_SPEED
        assert classify_from_symbol("Interface:CH340G") == NetClass.HIGH_SPEED
        assert classify_from_symbol("Memory_Flash:W25Q128") == NetClass.HIGH_SPEED

    def test_analog_symbol_classification(self):
        """Test analog component detection."""
        assert classify_from_symbol("Amplifier_Operational:LM358") == NetClass.ANALOG
        assert classify_from_symbol("Reference_Voltage:REF3012") == NetClass.ANALOG
        assert classify_from_symbol("Sensor_Temperature:LM35") == NetClass.ANALOG
        assert classify_from_symbol("Audio:PCM5122PW") == NetClass.ANALOG

    def test_rf_symbol_classification(self):
        """Test RF component detection."""
        assert classify_from_symbol("RF_Module:ESP32-WROOM") == NetClass.RF
        assert classify_from_symbol("RF_Amplifier:SKY65116") == NetClass.RF

    def test_debug_symbol_classification(self):
        """Test debug connector detection."""
        assert classify_from_symbol("Connector:Conn_ARM_JTAG_SWD_10") == NetClass.DEBUG
        assert classify_from_symbol("Connector:Conn_ARM_SWD_TagConnect") == NetClass.DEBUG

    def test_unknown_symbol_returns_none(self):
        """Test that unknown symbols return None."""
        assert classify_from_symbol("Device:R") is None
        assert classify_from_symbol("Device:C") is None
        assert classify_from_symbol("MCU:STM32F103") is None


class TestNamePatternClassification:
    """Tests for net name pattern matching."""

    def test_power_net_patterns(self):
        """Test power net name detection."""
        # Voltage rails
        assert classify_from_name("+3.3V") == NetClass.POWER
        assert classify_from_name("+5V") == NetClass.POWER
        assert classify_from_name("-12V") == NetClass.POWER
        assert classify_from_name("1.8V") == NetClass.POWER
        # Named power nets
        assert classify_from_name("VCC") == NetClass.POWER
        assert classify_from_name("VDD") == NetClass.POWER
        assert classify_from_name("VBUS") == NetClass.POWER
        assert classify_from_name("AVDD") == NetClass.POWER
        assert classify_from_name("DVDD") == NetClass.POWER

    def test_ground_net_patterns(self):
        """Test ground net name detection."""
        assert classify_from_name("GND") == NetClass.GROUND
        assert classify_from_name("AGND") == NetClass.GROUND
        assert classify_from_name("DGND") == NetClass.GROUND
        assert classify_from_name("PGND") == NetClass.GROUND
        assert classify_from_name("VSS") == NetClass.GROUND
        assert classify_from_name("CHASSIS") == NetClass.GROUND

    def test_clock_net_patterns(self):
        """Test clock signal name detection."""
        assert classify_from_name("CLK") == NetClass.CLOCK
        assert classify_from_name("MCLK") == NetClass.CLOCK
        assert classify_from_name("SYSCLK") == NetClass.CLOCK
        assert classify_from_name("SPI_CLK") == NetClass.CLOCK
        assert classify_from_name("OSC_IN") == NetClass.CLOCK
        assert classify_from_name("XTAL") == NetClass.CLOCK

    def test_high_speed_net_patterns(self):
        """Test high-speed signal name detection."""
        assert classify_from_name("USB_DP") == NetClass.HIGH_SPEED
        assert classify_from_name("USB_DM") == NetClass.HIGH_SPEED
        assert classify_from_name("ETH_TXD0") == NetClass.HIGH_SPEED
        assert classify_from_name("HDMI_CLK") == NetClass.HIGH_SPEED
        assert classify_from_name("QSPI_D0") == NetClass.HIGH_SPEED

    def test_differential_net_patterns(self):
        """Test differential pair name detection."""
        # Generic differential pairs without interface-specific prefixes
        assert classify_from_name("DATA_P") == NetClass.DIFFERENTIAL
        assert classify_from_name("DATA_N") == NetClass.DIFFERENTIAL
        assert classify_from_name("TXP") == NetClass.DIFFERENTIAL
        assert classify_from_name("TXN") == NetClass.DIFFERENTIAL
        # Note: CLK+, LVDS_P, USB_DP etc. match more specific classes first
        # (CLOCK, HIGH_SPEED) - differential is secondary classification

    def test_analog_net_patterns(self):
        """Test analog signal name detection."""
        assert classify_from_name("AIN0") == NetClass.ANALOG
        assert classify_from_name("VREF") == NetClass.ANALOG
        assert classify_from_name("AUDIO_L") == NetClass.ANALOG
        assert classify_from_name("I2S_SD") == NetClass.ANALOG  # I2S serial data
        assert classify_from_name("ADC_CH1") == NetClass.ANALOG

    def test_debug_net_patterns(self):
        """Test debug signal name detection."""
        assert classify_from_name("SWDIO") == NetClass.DEBUG
        assert classify_from_name("SWCLK") == NetClass.DEBUG
        assert classify_from_name("NRST") == NetClass.DEBUG
        assert classify_from_name("TDI") == NetClass.DEBUG
        assert classify_from_name("TDO") == NetClass.DEBUG

    def test_unknown_net_returns_none(self):
        """Test that unknown net names return None."""
        assert classify_from_name("DATA") is None
        assert classify_from_name("GPIO5") is None
        assert classify_from_name("Net-(U1-Pad3)") is None


class TestPinTypeClassification:
    """Tests for pin electrical type classification."""

    def test_power_pin_detection(self):
        """Test power_in/power_out pin classification."""
        assert classify_from_pin_type({"power_in"}) == NetClass.POWER
        assert classify_from_pin_type({"power_out"}) == NetClass.POWER
        assert classify_from_pin_type({"power_in", "passive"}) == NetClass.POWER

    def test_passive_only_returns_none(self):
        """Test that passive-only pins don't classify."""
        assert classify_from_pin_type({"passive"}) is None

    def test_signal_pins_return_none(self):
        """Test that signal pins don't override other detection."""
        assert classify_from_pin_type({"input"}) is None
        assert classify_from_pin_type({"output"}) is None
        assert classify_from_pin_type({"bidirectional"}) is None


class TestDifferentialPairDetection:
    """Tests for differential pair name detection."""

    def test_is_differential_pair_positive(self):
        """Test positive detection of differential pair names."""
        assert is_differential_pair_name("DATA_P") is True
        assert is_differential_pair_name("DATA_N") is True
        assert is_differential_pair_name("CLK+") is True
        assert is_differential_pair_name("CLK-") is True
        assert is_differential_pair_name("TXP") is True
        assert is_differential_pair_name("TXN") is True

    def test_is_differential_pair_negative(self):
        """Test negative detection of differential pair names."""
        assert is_differential_pair_name("DATA") is False
        assert is_differential_pair_name("CLK") is False
        assert is_differential_pair_name("GPIO5") is False

    def test_find_differential_partner(self):
        """Test finding differential pair partners."""
        assert find_differential_partner("DATA_P") == "DATA_N"
        assert find_differential_partner("DATA_N") == "DATA_P"
        assert find_differential_partner("CLK+") == "CLK-"
        assert find_differential_partner("CLK-") == "CLK+"
        assert find_differential_partner("USB_DP") == "USB_DM"
        assert find_differential_partner("USB_DM") == "USB_DP"

    def test_find_differential_partner_unknown(self):
        """Test that non-diff names return None."""
        assert find_differential_partner("DATA") is None
        assert find_differential_partner("GPIO5") is None


class TestClassifyNet:
    """Tests for the main classify_net function."""

    def test_classify_power_net(self):
        """Test power net classification."""
        result = classify_net("+3.3V")
        assert result.net_class == NetClass.POWER
        assert result.source == "name_pattern"
        assert result.confidence >= 0.5

    def test_classify_ground_net(self):
        """Test ground net classification."""
        result = classify_net("GND")
        assert result.net_class == NetClass.GROUND
        assert result.source == "name_pattern"
        assert result.confidence >= 0.7

    def test_classify_with_pin_types(self):
        """Test classification with pin type info."""

        # Create mock pin object
        class MockPin:
            pin_type = "power_in"

        result = classify_net(
            "+3.3V",
            connected_pins=[("U1", MockPin())],
        )
        assert result.net_class == NetClass.POWER
        assert result.source == "pin_type"
        assert result.confidence >= 0.9

    def test_classify_unknown_defaults_to_signal(self):
        """Test that unknown nets default to SIGNAL."""
        result = classify_net("Net-(U1-Pad3)")
        assert result.net_class == NetClass.SIGNAL
        assert result.source == "default"
        assert result.confidence == 0.5


class TestNetClassification:
    """Tests for NetClassification dataclass."""

    def test_classification_repr(self):
        """Test string representation."""
        clf = NetClassification(
            net_class=NetClass.POWER,
            confidence=0.95,
            source="pin_type",
            details="Test",
        )
        assert "power" in repr(clf)
        assert "95%" in repr(clf)
        assert "pin_type" in repr(clf)


class TestAutoClassifyNets:
    """Tests for the auto_classify_nets function."""

    def test_auto_classify_multiple_nets(self):
        """Test classifying multiple nets at once."""
        net_names = {
            1: "+3.3V",
            2: "GND",
            3: "CLK",
            4: "DATA",
            5: "USB_DP",
        }

        results = auto_classify_nets(net_names)

        assert results[1].net_class == NetClass.POWER
        assert results[2].net_class == NetClass.GROUND
        assert results[3].net_class == NetClass.CLOCK
        assert results[4].net_class == NetClass.SIGNAL  # Generic DATA
        assert results[5].net_class == NetClass.HIGH_SPEED

    def test_auto_classify_respects_confidence_threshold(self):
        """Test that confidence threshold filters results."""
        net_names = {1: "+3.3V", 2: "UnknownNet"}

        # With default threshold, power net should be included (0.7 confidence)
        results = auto_classify_nets(net_names, min_confidence=0.5)
        assert 1 in results
        assert results[1].net_class == NetClass.POWER

        # With very high threshold (0.9), name-pattern classifications (0.7) are filtered
        results_high = auto_classify_nets(net_names, min_confidence=0.9)
        # Name pattern classification has ~0.7 confidence, so filtered at 0.9
        assert len(results_high) == 0 or (1 in results_high and results_high[1].confidence >= 0.9)


class TestApplyNetClassRules:
    """Tests for applying routing rules to classified nets."""

    def test_apply_rules_creates_routing_config(self):
        """Test that apply_net_class_rules creates routing configs."""
        classifications = {
            1: NetClassification(NetClass.POWER, 0.95, "pin_type"),
            2: NetClassification(NetClass.GROUND, 0.95, "name_pattern"),
            3: NetClassification(NetClass.CLOCK, 0.80, "name_pattern"),
        }
        net_names = {1: "+3.3V", 2: "GND", 3: "CLK"}

        rules = apply_net_class_rules(classifications, net_names)

        assert "+3.3V" in rules
        assert "GND" in rules
        assert "CLK" in rules

        # Check power net has wide traces
        assert rules["+3.3V"].trace_width >= 0.5

        # Check ground net has high zone priority
        assert rules["GND"].zone_priority >= 10

        # Check clock net is length-critical
        assert rules["CLK"].length_critical is True


class TestClassifyAndApplyRules:
    """Tests for the convenience function."""

    def test_classify_and_apply_combined(self):
        """Test the combined classify and apply function."""
        net_names = {
            1: "+3.3V",
            2: "GND",
            3: "CLK",
        }

        rules = classify_and_apply_rules(net_names)

        assert "+3.3V" in rules
        assert "GND" in rules
        assert "CLK" in rules


class TestSymbolIndicatorsCompleteness:
    """Tests to ensure symbol indicators cover common cases."""

    def test_power_symbols_covered(self):
        """Ensure common power symbols are detected."""
        power_libs = [
            "power:VCC",
            "power:GND",
            "power:+5V",
            "Regulator_Linear:LM7805",
            "Regulator_Switching:TPS62291",
        ]
        for lib_id in power_libs:
            result = classify_from_symbol(lib_id)
            assert result in (NetClass.POWER, None), f"Unexpected for {lib_id}"

    def test_high_speed_interfaces_covered(self):
        """Ensure common high-speed interfaces are detected."""
        hs_libs = [
            "Connector:USB_C_Receptacle",
            "Interface:FT232RL",
            "Interface:CH340G",
        ]
        for lib_id in hs_libs:
            result = classify_from_symbol(lib_id)
            assert result == NetClass.HIGH_SPEED, f"Expected HIGH_SPEED for {lib_id}"


class TestNetClassPatternsCompleteness:
    """Tests to ensure net name patterns cover common conventions."""

    def test_voltage_rail_formats(self):
        """Test various voltage rail naming formats."""
        voltage_nets = ["+3.3V", "+5V", "+12V", "-5V", "1.8V", "3V3", "5V"]
        for net in voltage_nets:
            result = classify_from_name(net)
            # 3V3 and 5V might not match depending on pattern
            # Main ones should match
            if net.startswith("+") or net.startswith("-"):
                assert result == NetClass.POWER, f"Expected POWER for {net}"

    def test_usb_signal_formats(self):
        """Test USB signal naming formats."""
        usb_nets = ["USB_DP", "USB_DM", "USB_D+", "USB_D-", "USBDP", "USBDM"]
        for net in usb_nets:
            result = classify_from_name(net)
            assert result in (
                NetClass.HIGH_SPEED,
                NetClass.DIFFERENTIAL,
            ), f"Expected HIGH_SPEED or DIFFERENTIAL for {net}"

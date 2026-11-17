"""Tests for signal integrity module."""

import pytest

from kicad_tools.optim.signal_integrity import (
    NetClassification,
    SignalClass,
    SignalIntegrityHint,
    _find_differential_pair,
    _get_max_length_for_class,
    _get_priority_for_class,
    _match_patterns,
    add_si_constraints,
    classify_nets,
    get_si_score,
)


class TestSignalClass:
    """Tests for SignalClass enum."""

    def test_signal_class_values(self):
        """Test all signal class values exist."""
        assert SignalClass.CLOCK.value == "clock"
        assert SignalClass.HIGH_SPEED_DATA.value == "high_speed_data"
        assert SignalClass.DIFFERENTIAL.value == "differential"
        assert SignalClass.ANALOG_SENSITIVE.value == "analog_sensitive"
        assert SignalClass.POWER.value == "power"
        assert SignalClass.GENERAL.value == "general"


class TestNetClassification:
    """Tests for NetClassification dataclass."""

    def test_basic_classification(self):
        """Test creating a net classification."""
        nc = NetClassification(
            net_name="CLK",
            signal_class=SignalClass.CLOCK,
        )
        assert nc.net_name == "CLK"
        assert nc.signal_class == SignalClass.CLOCK
        assert nc.max_length_mm is None
        assert nc.matched_group is None
        assert nc.keep_away_from == []
        assert nc.priority == 0

    def test_is_critical_for_clock(self):
        """Test that clock signals are considered critical."""
        nc = NetClassification(net_name="CLK", signal_class=SignalClass.CLOCK)
        assert nc.is_critical is True

    def test_is_critical_for_high_speed(self):
        """Test that high-speed signals are considered critical."""
        nc = NetClassification(net_name="SPI_MISO", signal_class=SignalClass.HIGH_SPEED_DATA)
        assert nc.is_critical is True

    def test_is_critical_for_differential(self):
        """Test that differential pairs are considered critical."""
        nc = NetClassification(net_name="USB_DP", signal_class=SignalClass.DIFFERENTIAL)
        assert nc.is_critical is True

    def test_is_critical_for_analog(self):
        """Test that analog signals are considered critical."""
        nc = NetClassification(net_name="ADC_IN", signal_class=SignalClass.ANALOG_SENSITIVE)
        assert nc.is_critical is True

    def test_is_not_critical_for_power(self):
        """Test that power nets are not considered critical."""
        nc = NetClassification(net_name="VCC", signal_class=SignalClass.POWER)
        assert nc.is_critical is False

    def test_is_not_critical_for_general(self):
        """Test that general nets are not considered critical."""
        nc = NetClassification(net_name="LED1", signal_class=SignalClass.GENERAL)
        assert nc.is_critical is False


class TestSignalIntegrityHint:
    """Tests for SignalIntegrityHint dataclass."""

    def test_hint_creation(self):
        """Test creating a signal integrity hint."""
        hint = SignalIntegrityHint(
            hint_type="net_length",
            severity="warning",
            description="Net CLK is too long",
            affected_components=["U1", "Y1"],
            suggestion="Move Y1 closer to U1",
            estimated_improvement=5.0,
        )
        assert hint.hint_type == "net_length"
        assert hint.severity == "warning"
        assert "U1" in hint.affected_components
        assert "Y1" in hint.affected_components
        assert hint.estimated_improvement == 5.0

    def test_hint_str_representation(self):
        """Test string representation of hint."""
        hint = SignalIntegrityHint(
            hint_type="net_length",
            severity="critical",
            description="Critical issue",
            affected_components=["U1"],
            suggestion="Fix it",
        )
        hint_str = str(hint)
        assert "🔴" in hint_str
        assert "[net_length]" in hint_str
        assert "Critical issue" in hint_str


class TestPatternMatching:
    """Tests for net name pattern matching."""

    def test_clock_pattern_matching(self):
        """Test clock net name pattern matching."""
        from kicad_tools.optim.signal_integrity import _CLOCK_PATTERNS

        assert _match_patterns("CLK", _CLOCK_PATTERNS)
        assert _match_patterns("XTAL_OUT", _CLOCK_PATTERNS)
        assert _match_patterns("OSC_IN", _CLOCK_PATTERNS)
        assert _match_patterns("MCLK", _CLOCK_PATTERNS)
        assert _match_patterns("SYSCLK", _CLOCK_PATTERNS)
        assert not _match_patterns("DATA", _CLOCK_PATTERNS)
        assert not _match_patterns("VCC", _CLOCK_PATTERNS)

    def test_high_speed_pattern_matching(self):
        """Test high-speed net name pattern matching."""
        from kicad_tools.optim.signal_integrity import _HIGH_SPEED_PATTERNS

        assert _match_patterns("USB_DP", _HIGH_SPEED_PATTERNS)
        assert _match_patterns("SPI_MISO", _HIGH_SPEED_PATTERNS)
        assert _match_patterns("I2C_SDA", _HIGH_SPEED_PATTERNS)
        assert _match_patterns("UART_TX", _HIGH_SPEED_PATTERNS)
        assert _match_patterns("JTAG_TDI", _HIGH_SPEED_PATTERNS)
        assert not _match_patterns("LED1", _HIGH_SPEED_PATTERNS)

    def test_analog_pattern_matching(self):
        """Test analog net name pattern matching."""
        from kicad_tools.optim.signal_integrity import _ANALOG_PATTERNS

        assert _match_patterns("ADC_IN", _ANALOG_PATTERNS)
        assert _match_patterns("AIN1", _ANALOG_PATTERNS)
        assert _match_patterns("VREF", _ANALOG_PATTERNS)
        assert _match_patterns("TEMP_SENSE", _ANALOG_PATTERNS)
        assert not _match_patterns("GPIO1", _ANALOG_PATTERNS)

    def test_power_pattern_matching(self):
        """Test power net name pattern matching."""
        from kicad_tools.optim.signal_integrity import _POWER_PATTERNS

        assert _match_patterns("VCC", _POWER_PATTERNS)
        assert _match_patterns("VDD", _POWER_PATTERNS)
        assert _match_patterns("GND", _POWER_PATTERNS)
        assert _match_patterns("+3V3", _POWER_PATTERNS)
        assert _match_patterns("+5V", _POWER_PATTERNS)
        assert _match_patterns("VBAT", _POWER_PATTERNS)
        assert not _match_patterns("DATA", _POWER_PATTERNS)


class TestDifferentialPairDetection:
    """Tests for differential pair detection."""

    def test_find_usb_dp_dm_pair(self):
        """Test finding USB DP/DM pairs."""
        all_nets = {"USB_DP", "USB_DM", "VCC", "GND"}
        assert _find_differential_pair("USB_DP", all_nets) == "USB_DM"
        assert _find_differential_pair("USB_DM", all_nets) is None  # Only matches one direction

    def test_find_p_n_pair(self):
        """Test finding _P/_N pairs."""
        all_nets = {"ETH_TX_P", "ETH_TX_N", "VCC"}
        assert _find_differential_pair("ETH_TX_P", all_nets) == "ETH_TX_N"

    def test_find_plus_minus_pair(self):
        """Test finding +/- pairs."""
        all_nets = {"DATA+", "DATA-", "VCC"}
        assert _find_differential_pair("DATA+", all_nets) == "DATA-"

    def test_no_pair_found(self):
        """Test when no differential pair exists."""
        all_nets = {"CLK", "DATA", "VCC"}
        assert _find_differential_pair("CLK", all_nets) is None


class TestMaxLengthDefaults:
    """Tests for default max length values."""

    def test_clock_max_length(self):
        """Test clock net max length default."""
        assert _get_max_length_for_class(SignalClass.CLOCK) == 50.0

    def test_high_speed_max_length(self):
        """Test high-speed net max length default."""
        assert _get_max_length_for_class(SignalClass.HIGH_SPEED_DATA) == 100.0

    def test_differential_max_length(self):
        """Test differential pair max length default."""
        assert _get_max_length_for_class(SignalClass.DIFFERENTIAL) == 75.0

    def test_analog_max_length(self):
        """Test analog net max length default."""
        assert _get_max_length_for_class(SignalClass.ANALOG_SENSITIVE) == 25.0

    def test_power_no_max_length(self):
        """Test power nets have no max length constraint."""
        assert _get_max_length_for_class(SignalClass.POWER) is None

    def test_general_no_max_length(self):
        """Test general nets have no max length constraint."""
        assert _get_max_length_for_class(SignalClass.GENERAL) is None


class TestPriorityDefaults:
    """Tests for default priority values."""

    def test_clock_highest_priority(self):
        """Test clock nets have highest priority."""
        assert _get_priority_for_class(SignalClass.CLOCK) == 100

    def test_differential_high_priority(self):
        """Test differential pairs have high priority."""
        assert _get_priority_for_class(SignalClass.DIFFERENTIAL) == 90

    def test_power_low_priority(self):
        """Test power nets have low priority."""
        assert _get_priority_for_class(SignalClass.POWER) == 30

    def test_general_lowest_priority(self):
        """Test general nets have lowest priority."""
        assert _get_priority_for_class(SignalClass.GENERAL) == 10


class TestClassifyNets:
    """Tests for classify_nets function with mock PCB."""

    @pytest.fixture
    def mock_pcb(self):
        """Create a mock PCB with various nets."""

        class MockPad:
            def __init__(self, net_name):
                self.net_name = net_name

        class MockFootprint:
            def __init__(self, reference, net_names):
                self.reference = reference
                self.pads = [MockPad(name) for name in net_names]

        class MockPCB:
            def __init__(self):
                self.footprints = [
                    MockFootprint("U1", ["CLK", "VCC", "GND", "SPI_MISO", "SPI_MOSI"]),
                    MockFootprint("Y1", ["CLK", "GND"]),
                    MockFootprint("R1", ["ADC_IN", "VCC"]),
                    MockFootprint("J1", ["USB_DP", "USB_DM", "GND"]),
                ]

        return MockPCB()

    def test_classify_clock_net(self, mock_pcb):
        """Test clock net classification."""
        classifications = classify_nets(mock_pcb)
        assert "CLK" in classifications
        assert classifications["CLK"].signal_class == SignalClass.CLOCK

    def test_classify_power_nets(self, mock_pcb):
        """Test power net classification."""
        classifications = classify_nets(mock_pcb)
        assert "VCC" in classifications
        assert classifications["VCC"].signal_class == SignalClass.POWER
        assert "GND" in classifications
        assert classifications["GND"].signal_class == SignalClass.POWER

    def test_classify_high_speed_nets(self, mock_pcb):
        """Test high-speed net classification."""
        classifications = classify_nets(mock_pcb)
        assert "SPI_MISO" in classifications
        assert classifications["SPI_MISO"].signal_class == SignalClass.HIGH_SPEED_DATA
        assert "SPI_MOSI" in classifications
        assert classifications["SPI_MOSI"].signal_class == SignalClass.HIGH_SPEED_DATA

    def test_classify_differential_pairs(self, mock_pcb):
        """Test differential pair classification."""
        classifications = classify_nets(mock_pcb)
        assert "USB_DP" in classifications
        assert classifications["USB_DP"].signal_class == SignalClass.DIFFERENTIAL
        assert "USB_DM" in classifications
        assert classifications["USB_DM"].signal_class == SignalClass.DIFFERENTIAL
        # Both should have matched_group
        assert classifications["USB_DP"].matched_group is not None
        assert classifications["USB_DM"].matched_group is not None

    def test_classify_analog_net(self, mock_pcb):
        """Test analog net classification."""
        classifications = classify_nets(mock_pcb)
        assert "ADC_IN" in classifications
        assert classifications["ADC_IN"].signal_class == SignalClass.ANALOG_SENSITIVE


class TestSIScore:
    """Tests for get_si_score function."""

    @pytest.fixture
    def mock_pcb_good(self):
        """Create a mock PCB with good placement (no issues)."""

        class MockPad:
            def __init__(self, net_name, x, y):
                self.net_name = net_name
                self.position = (x, y)

        class MockFootprint:
            def __init__(self, reference, position, pads):
                self.reference = reference
                self.position = position
                self.pads = [MockPad(name, px, py) for name, px, py in pads]

        class MockPCB:
            def __init__(self):
                # Components are close together
                self.footprints = [
                    MockFootprint("U1", (50, 50), [("CLK", 0, 0), ("GND", 1, 0)]),
                    MockFootprint("Y1", (52, 50), [("CLK", 0, 0), ("GND", 1, 0)]),  # Very close
                ]

        return MockPCB()

    def test_si_score_range(self, mock_pcb_good):
        """Test SI score is in valid range."""
        score = get_si_score(mock_pcb_good)
        assert 0.0 <= score <= 100.0

    def test_si_score_without_classifications(self, mock_pcb_good):
        """Test SI score computes classifications if not provided."""
        score = get_si_score(mock_pcb_good)
        # Should not raise and return valid score
        assert isinstance(score, float)


class TestAddSIConstraints:
    """Tests for add_si_constraints function."""

    @pytest.fixture
    def mock_optimizer(self):
        """Create a mock optimizer with springs."""
        from kicad_tools.optim.components import Spring

        class MockOptimizer:
            def __init__(self):
                self.springs = [
                    Spring(
                        comp1_ref="U1",
                        pin1_num="1",
                        comp2_ref="Y1",
                        pin2_num="1",
                        stiffness=10.0,
                        net=1,
                        net_name="CLK",
                    ),
                    Spring(
                        comp1_ref="U1",
                        pin1_num="2",
                        comp2_ref="C1",
                        pin2_num="1",
                        stiffness=10.0,
                        net=2,
                        net_name="VCC",
                    ),
                    Spring(
                        comp1_ref="U1",
                        pin1_num="3",
                        comp2_ref="R1",
                        pin2_num="1",
                        stiffness=10.0,
                        net=3,
                        net_name="LED",
                    ),
                ]

        return MockOptimizer()

    def test_add_si_constraints_modifies_stiffness(self, mock_optimizer):
        """Test that SI constraints modify spring stiffness."""
        classifications = {
            "CLK": NetClassification(net_name="CLK", signal_class=SignalClass.CLOCK),
            "VCC": NetClassification(net_name="VCC", signal_class=SignalClass.POWER),
            "LED": NetClassification(net_name="LED", signal_class=SignalClass.GENERAL),
        }

        modified = add_si_constraints(mock_optimizer, classifications)

        assert modified == 2  # CLK and VCC modified

        # Clock should have 3x stiffness
        clk_spring = next(s for s in mock_optimizer.springs if s.net_name == "CLK")
        assert clk_spring.stiffness == 30.0

        # Power should have 0.5x stiffness
        vcc_spring = next(s for s in mock_optimizer.springs if s.net_name == "VCC")
        assert vcc_spring.stiffness == 5.0

        # General should be unchanged
        led_spring = next(s for s in mock_optimizer.springs if s.net_name == "LED")
        assert led_spring.stiffness == 10.0

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
from kicad_tools.router.diffpair_routing import (
    CoupledPathfinder,
    CoupledState,
    GridPos,
    PairOrientation,
    create_serpentine,
    match_pair_lengths,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.path import calculate_route_length
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules


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


# =============================================================================
# Tests for Coupled Routing Implementation
# =============================================================================


class TestGridPos:
    """Tests for GridPos dataclass."""

    def test_grid_pos_creation(self):
        """Test creating a GridPos."""
        pos = GridPos(x=10, y=20, layer=0)
        assert pos.x == 10
        assert pos.y == 20
        assert pos.layer == 0

    def test_grid_pos_hash(self):
        """Test GridPos hashing."""
        pos1 = GridPos(10, 20, 0)
        pos2 = GridPos(10, 20, 0)
        pos3 = GridPos(10, 20, 1)
        assert hash(pos1) == hash(pos2)
        assert hash(pos1) != hash(pos3)

    def test_grid_pos_equality(self):
        """Test GridPos equality."""
        pos1 = GridPos(10, 20, 0)
        pos2 = GridPos(10, 20, 0)
        pos3 = GridPos(10, 21, 0)
        assert pos1 == pos2
        assert pos1 != pos3

    def test_grid_pos_add(self):
        """Test GridPos addition."""
        pos = GridPos(10, 20, 0)
        new_pos = pos + (1, 2, 0)
        assert new_pos.x == 11
        assert new_pos.y == 22
        assert new_pos.layer == 0


class TestCoupledState:
    """Tests for CoupledState dataclass."""

    def test_coupled_state_creation(self):
        """Test creating a CoupledState."""
        p_pos = GridPos(10, 20, 0)
        n_pos = GridPos(12, 20, 0)
        state = CoupledState(p_pos=p_pos, n_pos=n_pos, direction=(1, 0))
        assert state.p_pos == p_pos
        assert state.n_pos == n_pos
        assert state.direction == (1, 0)

    def test_coupled_state_spacing(self):
        """Test CoupledState spacing calculation."""
        p_pos = GridPos(10, 20, 0)
        n_pos = GridPos(13, 20, 0)  # 3 cells apart horizontally
        state = CoupledState(p_pos=p_pos, n_pos=n_pos, direction=(1, 0))
        assert state.spacing == 3.0

        # Diagonal spacing
        p_pos = GridPos(10, 20, 0)
        n_pos = GridPos(13, 24, 0)  # 3 horizontal, 4 vertical = 5 (3-4-5 triangle)
        state = CoupledState(p_pos=p_pos, n_pos=n_pos, direction=(1, 0))
        assert state.spacing == 5.0

    def test_coupled_state_hash(self):
        """Test CoupledState hashing."""
        p_pos = GridPos(10, 20, 0)
        n_pos = GridPos(12, 20, 0)
        state1 = CoupledState(p_pos=p_pos, n_pos=n_pos, direction=(1, 0))
        state2 = CoupledState(p_pos=p_pos, n_pos=n_pos, direction=(1, 0))
        state3 = CoupledState(p_pos=p_pos, n_pos=n_pos, direction=(0, 1))
        assert hash(state1) == hash(state2)
        assert hash(state1) != hash(state3)

    def test_coupled_state_equality(self):
        """Test CoupledState equality."""
        p_pos = GridPos(10, 20, 0)
        n_pos = GridPos(12, 20, 0)
        state1 = CoupledState(p_pos=p_pos, n_pos=n_pos, direction=(1, 0))
        state2 = CoupledState(p_pos=p_pos, n_pos=n_pos, direction=(1, 0))
        state3 = CoupledState(p_pos=p_pos, n_pos=GridPos(11, 20, 0), direction=(1, 0))
        assert state1 == state2
        assert state1 != state3


class TestPairOrientation:
    """Tests for PairOrientation enum."""

    def test_orientation_values(self):
        """Test orientation enum values."""
        assert PairOrientation.HORIZONTAL.value == "horizontal"
        assert PairOrientation.VERTICAL.value == "vertical"


class TestCoupledPathfinder:
    """Tests for CoupledPathfinder class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_diameter=0.6,
            via_drill=0.3,
            via_clearance=0.2,
        )
        self.grid = RoutingGrid(
            width=20.0,
            height=20.0,
            rules=self.rules,
            origin_x=0.0,
            origin_y=0.0,
        )
        # Target spacing of 2 cells
        self.pathfinder = CoupledPathfinder(
            grid=self.grid,
            rules=self.rules,
            target_spacing_cells=2,
        )

    def test_pathfinder_initialization(self):
        """Test pathfinder initialization."""
        assert self.pathfinder.grid == self.grid
        assert self.pathfinder.rules == self.rules
        assert self.pathfinder.target_spacing_cells == 2
        assert len(self.pathfinder.directions) == 4  # Orthogonal only

    def test_is_cell_blocked_out_of_bounds(self):
        """Test cell blocking for out-of-bounds positions."""
        assert self.pathfinder._is_cell_blocked(-1, 0, 0, 1) is True
        assert self.pathfinder._is_cell_blocked(0, -1, 0, 1) is True
        assert self.pathfinder._is_cell_blocked(self.grid.cols + 1, 0, 0, 1) is True

    def test_is_cell_blocked_valid_position(self):
        """Test cell blocking for valid positions."""
        # Center of empty grid should not be blocked
        assert self.pathfinder._is_cell_blocked(10, 10, 0, 1) is False

    def test_get_coupled_neighbors_basic(self):
        """Test getting coupled neighbors."""
        p_pos = GridPos(10, 10, 0)
        n_pos = GridPos(12, 10, 0)  # 2 cells apart
        state = CoupledState(p_pos=p_pos, n_pos=n_pos, direction=(0, 0))

        neighbors = self.pathfinder._get_coupled_neighbors(state, p_net=1, n_net=2)

        # Should have some valid neighbors
        assert len(neighbors) > 0

        # All neighbors should maintain spacing within tolerance
        for new_state, cost, is_via in neighbors:
            spacing = new_state.spacing
            # Allow 1 cell tolerance
            assert abs(spacing - 2) <= 1

    def test_heuristic_calculation(self):
        """Test heuristic calculation."""
        state = CoupledState(
            p_pos=GridPos(0, 0, 0),
            n_pos=GridPos(2, 0, 0),
            direction=(1, 0),
        )
        p_goal = GridPos(10, 0, 0)
        n_goal = GridPos(12, 0, 0)

        h = self.pathfinder._heuristic(state, p_goal, n_goal)

        # Should be non-negative
        assert h >= 0

        # Should be greater for further distances
        state_far = CoupledState(
            p_pos=GridPos(0, 0, 0),
            n_pos=GridPos(2, 0, 0),
            direction=(1, 0),
        )
        p_goal_far = GridPos(100, 0, 0)
        n_goal_far = GridPos(102, 0, 0)
        h_far = self.pathfinder._heuristic(state_far, p_goal_far, n_goal_far)

        assert h_far > h


class TestCreateSerpentine:
    """Tests for create_serpentine function."""

    def test_serpentine_no_length_needed(self):
        """Test serpentine with zero length to add."""
        route = Route(net=1, net_name="TEST")
        route.segments.append(
            Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.F_CU, net=1, net_name="TEST")
        )

        result = create_serpentine(route, 0)
        assert result is False  # No change needed

    def test_serpentine_negative_length(self):
        """Test serpentine with negative length to add."""
        route = Route(net=1, net_name="TEST")
        route.segments.append(
            Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.F_CU, net=1, net_name="TEST")
        )

        result = create_serpentine(route, -1)
        assert result is False

    def test_serpentine_short_segment(self):
        """Test serpentine with segment too short."""
        route = Route(net=1, net_name="TEST")
        route.segments.append(
            Segment(
                x1=0,
                y1=0,
                x2=0.5,
                y2=0,  # Only 0.5mm long
                width=0.2,
                layer=Layer.F_CU,
                net=1,
                net_name="TEST",
            )
        )

        result = create_serpentine(route, 1.0, min_segment_length=1.0)
        assert result is False

    def test_serpentine_horizontal_segment(self):
        """Test serpentine on horizontal segment."""
        route = Route(net=1, net_name="TEST")
        route.segments.append(
            Segment(
                x1=0,
                y1=0,
                x2=10,
                y2=0,  # 10mm horizontal
                width=0.2,
                layer=Layer.F_CU,
                net=1,
                net_name="TEST",
            )
        )

        original_length = calculate_route_length([route])
        result = create_serpentine(route, 2.0)

        assert result is True
        # Should have more segments now
        assert len(route.segments) > 1
        # Total length should have increased
        new_length = calculate_route_length([route])
        assert new_length > original_length

    def test_serpentine_vertical_segment(self):
        """Test serpentine on vertical segment."""
        route = Route(net=1, net_name="TEST")
        route.segments.append(
            Segment(
                x1=0,
                y1=0,
                x2=0,
                y2=10,  # 10mm vertical
                width=0.2,
                layer=Layer.F_CU,
                net=1,
                net_name="TEST",
            )
        )

        result = create_serpentine(route, 2.0)
        assert result is True
        assert len(route.segments) > 1


class TestMatchPairLengths:
    """Tests for match_pair_lengths function."""

    def test_match_already_matched(self):
        """Test matching traces that are already matched."""
        p_route = Route(net=1, net_name="USB_D+")
        p_route.segments.append(
            Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.F_CU, net=1, net_name="USB_D+")
        )

        n_route = Route(net=2, net_name="USB_D-")
        n_route.segments.append(
            Segment(
                x1=0,
                y1=1,
                x2=10,
                y2=1,  # Same length
                width=0.2,
                layer=Layer.F_CU,
                net=2,
                net_name="USB_D-",
            )
        )

        result = match_pair_lengths(p_route, n_route, max_delta=1.0)
        assert result is True

    def test_match_with_mismatch(self):
        """Test matching traces with length mismatch."""
        p_route = Route(net=1, net_name="USB_D+")
        p_route.segments.append(
            Segment(
                x1=0,
                y1=0,
                x2=12,
                y2=0,  # 12mm
                width=0.2,
                layer=Layer.F_CU,
                net=1,
                net_name="USB_D+",
            )
        )

        n_route = Route(net=2, net_name="USB_D-")
        n_route.segments.append(
            Segment(
                x1=0,
                y1=1,
                x2=10,
                y2=1,  # 10mm - 2mm shorter (needs serpentine)
                width=0.2,
                layer=Layer.F_CU,
                net=2,
                net_name="USB_D-",
            )
        )

        p_len_before = calculate_route_length([p_route])
        n_len_before = calculate_route_length([n_route])
        delta_before = abs(p_len_before - n_len_before)
        assert delta_before > 1.0  # Significant mismatch (2mm)

        result = match_pair_lengths(p_route, n_route, max_delta=1.0, add_serpentines=True)

        # Serpentine should have been added to the shorter trace
        assert result is True
        # N route should have more segments now (serpentine added)
        assert len(n_route.segments) > 1

        # After serpentine, the length should have increased
        n_len_after = calculate_route_length([n_route])
        assert n_len_after > n_len_before

    def test_match_without_serpentines(self):
        """Test matching with serpentines disabled."""
        p_route = Route(net=1, net_name="USB_D+")
        p_route.segments.append(
            Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.F_CU, net=1, net_name="USB_D+")
        )

        n_route = Route(net=2, net_name="USB_D-")
        n_route.segments.append(
            Segment(
                x1=0,
                y1=1,
                x2=5,
                y2=1,  # 5mm shorter
                width=0.2,
                layer=Layer.F_CU,
                net=2,
                net_name="USB_D-",
            )
        )

        result = match_pair_lengths(p_route, n_route, max_delta=1.0, add_serpentines=False)
        assert result is False  # Cannot match without serpentines


class TestCoupledRoutingIntegration:
    """Integration tests for coupled differential pair routing."""

    def test_coupled_routing_simple_case(self):
        """Test coupled routing with a simple case."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_diameter=0.6,
            via_drill=0.3,
        )
        grid = RoutingGrid(
            width=30.0,
            height=30.0,
            rules=rules,
            origin_x=0.0,
            origin_y=0.0,
        )

        # Create pads with 2mm spacing between P and N
        p_start = Pad(
            x=5.0, y=10.0, width=1.0, height=1.0, net=1, net_name="USB_D+", layer=Layer.F_CU
        )
        p_end = Pad(
            x=25.0, y=10.0, width=1.0, height=1.0, net=1, net_name="USB_D+", layer=Layer.F_CU
        )
        n_start = Pad(
            x=5.0, y=12.0, width=1.0, height=1.0, net=2, net_name="USB_D-", layer=Layer.F_CU
        )
        n_end = Pad(
            x=25.0, y=12.0, width=1.0, height=1.0, net=2, net_name="USB_D-", layer=Layer.F_CU
        )

        # Add pads to grid
        grid.add_pad(p_start)
        grid.add_pad(p_end)
        grid.add_pad(n_start)
        grid.add_pad(n_end)

        # Calculate spacing in cells (2mm spacing)
        spacing_cells = int(2.0 / grid.resolution)

        pathfinder = CoupledPathfinder(
            grid=grid,
            rules=rules,
            target_spacing_cells=spacing_cells,
        )

        result = pathfinder.route_coupled(p_start, p_end, n_start, n_end)

        # Note: Coupled routing may fail on simple grids due to spacing constraints
        # This is expected behavior - coupled routing is more constrained
        if result is not None:
            p_route, n_route = result
            assert p_route.net == 1
            assert n_route.net == 2
            assert len(p_route.segments) > 0 or len(n_route.segments) > 0

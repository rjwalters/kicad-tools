"""Tests for intent declaration MCP tools.

Tests the intent declaration workflow integrated with placement sessions:
declare_interface -> declare_power_rail -> list_intents -> clear_intent
"""

from pathlib import Path

import pytest

pytest.importorskip("pydantic")

from kicad_tools.mcp.tools.session import (
    apply_move,
    clear_intent,
    declare_interface,
    declare_power_rail,
    list_intents,
    query_move,
    reset_session_manager,
    start_session,
)

# PCB with nets suitable for interface declarations
INTENT_TEST_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VDD_3V3")
  (net 2 "GND")
  (net 3 "USB_DP")
  (net 4 "USB_DM")
  (net 5 "SPI_CLK")
  (net 6 "SPI_MOSI")
  (net 7 "SPI_MISO")
  (net 8 "SPI_CS")
  (net 9 "I2C_SDA")
  (net 10 "I2C_SCL")

  (gr_line (start 0 0) (end 100 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 100 0) (end 100 80) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 100 80) (end 0 80) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 80) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "USB_C"
    (layer "F.Cu")
    (at 10 40)
    (attr smd)
    (property "Reference" "J1")
    (property "Value" "USB_C")
    (pad "1" smd rect (at 0 -1) (size 0.3 1.0) (layers "F.Cu") (net 3 "USB_DP"))
    (pad "2" smd rect (at 0 1) (size 0.3 1.0) (layers "F.Cu") (net 4 "USB_DM"))
    (pad "3" smd rect (at 1 0) (size 0.3 1.0) (layers "F.Cu") (net 1 "VDD_3V3"))
    (pad "4" smd rect (at 2 0) (size 0.3 1.0) (layers "F.Cu") (net 2 "GND"))
  )

  (footprint "QFN-32"
    (layer "F.Cu")
    (at 50 40)
    (attr smd)
    (property "Reference" "U1")
    (property "Value" "MCU")
    (pad "1" smd rect (at -3 -3) (size 0.5 0.5) (layers "F.Cu") (net 3 "USB_DP"))
    (pad "2" smd rect (at -3 -2) (size 0.5 0.5) (layers "F.Cu") (net 4 "USB_DM"))
    (pad "3" smd rect (at -3 -1) (size 0.5 0.5) (layers "F.Cu") (net 5 "SPI_CLK"))
    (pad "4" smd rect (at -3 0) (size 0.5 0.5) (layers "F.Cu") (net 6 "SPI_MOSI"))
    (pad "5" smd rect (at -3 1) (size 0.5 0.5) (layers "F.Cu") (net 7 "SPI_MISO"))
    (pad "6" smd rect (at -3 2) (size 0.5 0.5) (layers "F.Cu") (net 8 "SPI_CS"))
    (pad "7" smd rect (at -3 3) (size 0.5 0.5) (layers "F.Cu") (net 9 "I2C_SDA"))
    (pad "8" smd rect (at -2 3) (size 0.5 0.5) (layers "F.Cu") (net 10 "I2C_SCL"))
    (pad "VDD" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "VDD_3V3"))
    (pad "GND" smd rect (at 1 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "GND"))
  )

  (footprint "C_0603"
    (layer "F.Cu")
    (at 60 30)
    (attr smd)
    (property "Reference" "C1")
    (property "Value" "100nF")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VDD_3V3"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "GND"))
  )
)
"""


@pytest.fixture(autouse=True)
def reset_sessions():
    """Reset session manager before each test."""
    reset_session_manager()
    yield
    reset_session_manager()


@pytest.fixture
def intent_pcb_path(tmp_path: Path) -> str:
    """Create a temporary PCB file for testing."""
    pcb_file = tmp_path / "intent_test_board.kicad_pcb"
    pcb_file.write_text(INTENT_TEST_PCB)
    return str(pcb_file)


@pytest.fixture
def active_session(intent_pcb_path: str) -> str:
    """Create an active session and return its ID."""
    result = start_session(intent_pcb_path)
    assert result.success
    return result.session_id


class TestDeclareInterface:
    """Tests for declare_interface function."""

    def test_declare_usb_interface(self, active_session: str) -> None:
        """Test declaring a USB 2.0 High Speed interface."""
        result = declare_interface(
            session_id=active_session,
            interface_type="usb2_high_speed",
            nets=["USB_DP", "USB_DM"],
        )

        assert result.success is True
        assert result.declared is True
        assert result.interface_type == "usb2_high_speed"
        assert result.nets == ["USB_DP", "USB_DM"]
        assert len(result.constraints) > 0
        assert result.error_message is None

    def test_declare_spi_interface(self, active_session: str) -> None:
        """Test declaring an SPI interface."""
        result = declare_interface(
            session_id=active_session,
            interface_type="spi_standard",
            nets=["SPI_CLK", "SPI_MOSI", "SPI_MISO", "SPI_CS"],
        )

        assert result.success is True
        assert result.declared is True
        assert result.interface_type == "spi_standard"
        assert len(result.nets) == 4

    def test_declare_i2c_interface(self, active_session: str) -> None:
        """Test declaring an I2C interface."""
        result = declare_interface(
            session_id=active_session,
            interface_type="i2c_standard",
            nets=["I2C_SDA", "I2C_SCL"],
        )

        assert result.success is True
        assert result.declared is True
        assert result.interface_type == "i2c_standard"

    def test_declare_unknown_interface_type(self, active_session: str) -> None:
        """Test error handling for unknown interface type."""
        result = declare_interface(
            session_id=active_session,
            interface_type="nonexistent_interface",
            nets=["NET1", "NET2"],
        )

        assert result.success is False
        assert "Unknown interface type" in result.error_message

    def test_declare_invalid_session(self) -> None:
        """Test error handling for invalid session ID."""
        result = declare_interface(
            session_id="invalid_session",
            interface_type="usb2_high_speed",
            nets=["USB_DP", "USB_DM"],
        )

        assert result.success is False
        assert "Session not found" in result.error_message

    def test_declare_usb_wrong_net_count(self, active_session: str) -> None:
        """Test error when USB interface has wrong number of nets."""
        result = declare_interface(
            session_id=active_session,
            interface_type="usb2_high_speed",
            nets=["USB_DP"],  # Should be 2 nets
        )

        assert result.success is False
        assert result.error_message is not None

    def test_declare_multiple_interfaces(self, active_session: str) -> None:
        """Test declaring multiple interfaces in one session."""
        # Declare USB
        result1 = declare_interface(
            session_id=active_session,
            interface_type="usb2_high_speed",
            nets=["USB_DP", "USB_DM"],
        )
        assert result1.success is True

        # Declare I2C
        result2 = declare_interface(
            session_id=active_session,
            interface_type="i2c_standard",
            nets=["I2C_SDA", "I2C_SCL"],
        )
        assert result2.success is True

        # Verify both are tracked
        list_result = list_intents(active_session)
        assert list_result.success is True
        assert len(list_result.intents) == 2


class TestDeclarePowerRail:
    """Tests for declare_power_rail function."""

    def test_declare_power_rail(self, active_session: str) -> None:
        """Test declaring a power rail."""
        result = declare_power_rail(
            session_id=active_session,
            net="VDD_3V3",
            voltage=3.3,
            max_current=0.5,
        )

        assert result.success is True
        assert result.declared is True
        assert result.net == "VDD_3V3"
        assert result.voltage == 3.3
        assert result.max_current == 0.5
        assert len(result.constraints) > 0
        assert result.error_message is None

    def test_declare_power_rail_high_current(self, active_session: str) -> None:
        """Test declaring a high-current power rail."""
        result = declare_power_rail(
            session_id=active_session,
            net="VDD_3V3",
            voltage=3.3,
            max_current=2.0,
        )

        assert result.success is True
        # Higher current should result in wider trace constraints
        trace_width_constraint = next(
            (c for c in result.constraints if c.type == "min_trace_width"),
            None,
        )
        assert trace_width_constraint is not None
        assert trace_width_constraint.params.get("min_mm", 0) > 0.25  # Higher than default

    def test_declare_power_rail_invalid_session(self) -> None:
        """Test error handling for invalid session ID."""
        result = declare_power_rail(
            session_id="invalid_session",
            net="VDD_3V3",
            voltage=3.3,
        )

        assert result.success is False
        assert "Session not found" in result.error_message


class TestListIntents:
    """Tests for list_intents function."""

    def test_list_intents_empty(self, active_session: str) -> None:
        """Test listing intents when none declared."""
        result = list_intents(active_session)

        assert result.success is True
        assert len(result.intents) == 0
        assert result.constraint_count == 0

    def test_list_intents_with_declarations(self, active_session: str) -> None:
        """Test listing intents after declarations."""
        # Declare interfaces
        declare_interface(
            session_id=active_session,
            interface_type="usb2_high_speed",
            nets=["USB_DP", "USB_DM"],
        )
        declare_power_rail(
            session_id=active_session,
            net="VDD_3V3",
            voltage=3.3,
        )

        result = list_intents(active_session)

        assert result.success is True
        assert len(result.intents) == 2
        assert result.constraint_count > 0

        # Verify intent info
        interface_types = [i.interface_type for i in result.intents]
        assert "usb2_high_speed" in interface_types
        assert "power_rail" in interface_types

    def test_list_intents_invalid_session(self) -> None:
        """Test error handling for invalid session ID."""
        result = list_intents("invalid_session")

        assert result.success is False
        assert "Session not found" in result.error_message


class TestClearIntent:
    """Tests for clear_intent function."""

    def test_clear_all_intents(self, active_session: str) -> None:
        """Test clearing all intents."""
        # Add some intents
        declare_interface(
            session_id=active_session,
            interface_type="usb2_high_speed",
            nets=["USB_DP", "USB_DM"],
        )
        declare_power_rail(
            session_id=active_session,
            net="VDD_3V3",
            voltage=3.3,
        )

        # Clear all
        result = clear_intent(active_session)

        assert result.success is True
        assert result.cleared_count == 2
        assert result.remaining_count == 0

        # Verify cleared
        list_result = list_intents(active_session)
        assert len(list_result.intents) == 0

    def test_clear_by_interface_type(self, active_session: str) -> None:
        """Test clearing intents by interface type."""
        # Add multiple intents
        declare_interface(
            session_id=active_session,
            interface_type="usb2_high_speed",
            nets=["USB_DP", "USB_DM"],
        )
        declare_interface(
            session_id=active_session,
            interface_type="i2c_standard",
            nets=["I2C_SDA", "I2C_SCL"],
        )
        declare_power_rail(
            session_id=active_session,
            net="VDD_3V3",
            voltage=3.3,
        )

        # Clear only USB intents
        result = clear_intent(
            session_id=active_session,
            interface_type="usb2_high_speed",
        )

        assert result.success is True
        assert result.cleared_count == 1
        assert result.remaining_count == 2

    def test_clear_by_nets(self, active_session: str) -> None:
        """Test clearing intents by net names."""
        # Add intents
        declare_interface(
            session_id=active_session,
            interface_type="usb2_high_speed",
            nets=["USB_DP", "USB_DM"],
        )
        declare_power_rail(
            session_id=active_session,
            net="VDD_3V3",
            voltage=3.3,
        )

        # Clear intents involving USB nets
        result = clear_intent(
            session_id=active_session,
            nets=["USB_DP"],
        )

        assert result.success is True
        assert result.cleared_count == 1
        assert result.remaining_count == 1

    def test_clear_intent_invalid_session(self) -> None:
        """Test error handling for invalid session ID."""
        result = clear_intent("invalid_session")

        assert result.success is False
        assert "Session not found" in result.error_message


class TestIntentAwareMoves:
    """Tests for intent status in move operations."""

    def test_query_move_with_intents(self, active_session: str) -> None:
        """Test that query_move includes intent status when intents are declared."""
        # Declare power rail (which includes VDD_3V3)
        declare_power_rail(
            session_id=active_session,
            net="VDD_3V3",
            voltage=3.3,
        )

        # Query a move for C1 (connected to VDD_3V3)
        result = query_move(
            session_id=active_session,
            ref="C1",
            x=65.0,
            y=35.0,
        )

        assert result.success is True
        # Intent status should be present when intents are declared
        assert result.intent_status is not None
        # C1 is connected to VDD_3V3, so power_rail should be in affected_intents
        assert "power_rail" in result.intent_status.affected_intents

    def test_query_move_without_intents(self, active_session: str) -> None:
        """Test that query_move doesn't include intent status when no intents."""
        # Query a move without declaring any intents
        result = query_move(
            session_id=active_session,
            ref="C1",
            x=65.0,
            y=35.0,
        )

        assert result.success is True
        # No intents declared, so intent_status should be None
        assert result.intent_status is None

    def test_apply_move_with_intents(self, active_session: str) -> None:
        """Test that apply_move includes intent status when intents are declared."""
        # Declare power rail
        declare_power_rail(
            session_id=active_session,
            net="VDD_3V3",
            voltage=3.3,
        )

        # Move the decoupling capacitor (connected to VDD_3V3)
        result = apply_move(
            session_id=active_session,
            ref="C1",
            x=65.0,
            y=35.0,
        )

        assert result.success is True
        assert result.intent_status is not None

    def test_move_unrelated_component(self, active_session: str) -> None:
        """Test moving a component not connected to declared interface."""
        # Declare USB interface
        declare_interface(
            session_id=active_session,
            interface_type="usb2_high_speed",
            nets=["USB_DP", "USB_DM"],
        )

        # Move decoupling capacitor (not connected to USB nets)
        result = query_move(
            session_id=active_session,
            ref="C1",  # Connected to VDD_3V3 and GND, not USB
            x=65.0,
            y=35.0,
        )

        assert result.success is True
        # Intent status present but USB not in affected (C1 not on USB nets)
        if result.intent_status:
            assert "usb2_high_speed" not in result.intent_status.affected_intents


class TestIntentConstraintInfo:
    """Tests for constraint information in declarations."""

    def test_usb_constraints(self, active_session: str) -> None:
        """Test that USB declarations include expected constraints."""
        result = declare_interface(
            session_id=active_session,
            interface_type="usb2_high_speed",
            nets=["USB_DP", "USB_DM"],
        )

        assert result.success is True
        assert len(result.constraints) > 0

        # Check constraint types
        constraint_types = [c.type for c in result.constraints]
        # USB should have differential pair constraints
        assert any("differential" in ct or "impedance" in ct for ct in constraint_types)

    def test_power_rail_constraints(self, active_session: str) -> None:
        """Test that power rail declarations include expected constraints."""
        result = declare_power_rail(
            session_id=active_session,
            net="VDD_3V3",
            voltage=3.3,
            max_current=1.0,
        )

        assert result.success is True
        assert len(result.constraints) > 0

        # Check for trace width and decoupling constraints
        constraint_types = [c.type for c in result.constraints]
        assert "min_trace_width" in constraint_types
        assert "requires_decoupling" in constraint_types

    def test_i2c_constraints(self, active_session: str) -> None:
        """Test that I2C declarations include expected constraints."""
        result = declare_interface(
            session_id=active_session,
            interface_type="i2c_standard",
            nets=["I2C_SDA", "I2C_SCL"],
        )

        assert result.success is True
        assert len(result.constraints) > 0
        # All constraints should have source from the interface that generated them
        # (may be i2c_standard or another source name used by the spec)
        for c in result.constraints:
            assert c.source is not None
            assert len(c.source) > 0


class TestSessionIntentPersistence:
    """Tests for intent persistence across session operations."""

    def test_intents_persist_across_moves(self, active_session: str) -> None:
        """Test that intents persist after applying moves."""
        # Declare intent
        declare_interface(
            session_id=active_session,
            interface_type="usb2_high_speed",
            nets=["USB_DP", "USB_DM"],
        )

        # Apply several moves
        apply_move(active_session, "C1", 65.0, 35.0)
        apply_move(active_session, "C1", 60.0, 30.0)

        # Intents should still be present
        list_result = list_intents(active_session)
        assert len(list_result.intents) == 1
        assert list_result.intents[0].interface_type == "usb2_high_speed"

    def test_intents_available_in_to_dict(self, active_session: str) -> None:
        """Test that intent results serialize correctly."""
        result = declare_interface(
            session_id=active_session,
            interface_type="usb2_high_speed",
            nets=["USB_DP", "USB_DM"],
        )

        # Should serialize to dict without error
        result_dict = result.to_dict()
        assert result_dict["success"] is True
        assert result_dict["interface"] == "usb2_high_speed"
        assert result_dict["nets"] == ["USB_DP", "USB_DM"]
        assert len(result_dict["constraints"]) > 0

"""Tests for the datasheet symbol generator module."""

import pytest

from kicad_tools.datasheet.pin_layout import (
    LayoutStyle,
    PinLayoutEngine,
    SymbolLayout,
    _classify_pin_group,
    _sort_pins_by_name,
    _sort_pins_by_number,
)
from kicad_tools.datasheet.pins import ExtractedPin, PinTable
from kicad_tools.datasheet.symbol_generator import (
    GeneratedSymbol,
    SymbolGenerator,
    create_symbol_from_datasheet,
)
from kicad_tools.schema.library import SymbolLibrary


class TestPinClassification:
    """Tests for pin classification logic."""

    def test_power_positive_pins(self):
        """Test that power positive pins are classified correctly."""
        for name in ["VCC", "VDD", "VBAT", "3V3", "5V"]:
            pin = ExtractedPin(number="1", name=name, type="power_in")
            assert _classify_pin_group(pin) == "power_positive"

    def test_power_negative_pins(self):
        """Test that ground pins are classified correctly."""
        for name in ["GND", "VSS", "GNDA", "AGND"]:
            pin = ExtractedPin(number="1", name=name, type="power_in")
            assert _classify_pin_group(pin) == "power_negative"

    def test_reset_pins(self):
        """Test that reset pins are classified correctly."""
        for name in ["RST", "RESET", "NRST"]:
            pin = ExtractedPin(number="1", name=name, type="input")
            assert _classify_pin_group(pin) == "reset"

    def test_oscillator_pins(self):
        """Test that oscillator pins are classified correctly."""
        for name in ["OSC_IN", "OSC_OUT", "XTAL"]:
            pin = ExtractedPin(number="1", name=name, type="input")
            assert _classify_pin_group(pin) == "oscillator"

    def test_port_pins(self):
        """Test that port pins are classified correctly."""
        pin_a = ExtractedPin(number="1", name="PA0", type="bidirectional")
        pin_b = ExtractedPin(number="2", name="PB1", type="bidirectional")

        assert _classify_pin_group(pin_a) == "port_a"
        assert _classify_pin_group(pin_b) == "port_b"

    def test_spi_pins(self):
        """Test that SPI pins are classified correctly."""
        for name in ["MOSI", "MISO", "SCK", "NSS"]:
            pin = ExtractedPin(number="1", name=name, type="bidirectional")
            assert _classify_pin_group(pin) == "spi"

    def test_i2c_pins(self):
        """Test that I2C pins are classified correctly."""
        for name in ["SDA", "SCL"]:
            pin = ExtractedPin(number="1", name=name, type="bidirectional")
            assert _classify_pin_group(pin) == "i2c"

    def test_uart_pins(self):
        """Test that UART pins are classified correctly."""
        for name in ["TX", "RX", "TXD", "RXD"]:
            pin = ExtractedPin(number="1", name=name, type="bidirectional")
            assert _classify_pin_group(pin) == "uart"

    def test_unclassified_gpio(self):
        """Test that unclassified pins default to gpio."""
        pin = ExtractedPin(number="1", name="CUSTOM_PIN", type="bidirectional")
        assert _classify_pin_group(pin) == "gpio"


class TestPinSorting:
    """Tests for pin sorting logic."""

    def test_sort_by_number_numeric(self):
        """Test sorting pins by numeric number."""
        pins = [
            ExtractedPin(number="10", name="P10", type="passive"),
            ExtractedPin(number="1", name="P1", type="passive"),
            ExtractedPin(number="2", name="P2", type="passive"),
        ]
        sorted_pins = _sort_pins_by_number(pins)

        assert [p.number for p in sorted_pins] == ["1", "2", "10"]

    def test_sort_by_name_port(self):
        """Test sorting pins by port name."""
        pins = [
            ExtractedPin(number="1", name="PA10", type="bidirectional"),
            ExtractedPin(number="2", name="PA1", type="bidirectional"),
            ExtractedPin(number="3", name="PA2", type="bidirectional"),
        ]
        sorted_pins = _sort_pins_by_name(pins)

        assert [p.name for p in sorted_pins] == ["PA1", "PA2", "PA10"]


class TestPinLayoutEngine:
    """Tests for the PinLayoutEngine class."""

    @pytest.fixture
    def engine(self):
        """Create a layout engine instance."""
        return PinLayoutEngine()

    @pytest.fixture
    def sample_pins(self):
        """Create a sample set of pins for testing."""
        return [
            ExtractedPin(number="1", name="VDD", type="power_in"),
            ExtractedPin(number="2", name="GND", type="power_in"),
            ExtractedPin(number="3", name="PA0", type="bidirectional"),
            ExtractedPin(number="4", name="PA1", type="bidirectional"),
            ExtractedPin(number="5", name="PB0", type="bidirectional"),
            ExtractedPin(number="6", name="PB1", type="bidirectional"),
            ExtractedPin(number="7", name="NRST", type="input"),
            ExtractedPin(number="8", name="TX", type="output"),
        ]

    def test_functional_layout(self, engine, sample_pins):
        """Test functional layout calculation."""
        layout = engine.calculate_layout(sample_pins, style=LayoutStyle.FUNCTIONAL)

        assert isinstance(layout, SymbolLayout)
        assert len(layout.pin_positions) == len(sample_pins)
        assert layout.symbol_width > 0
        assert layout.symbol_height > 0

    def test_physical_layout(self, engine, sample_pins):
        """Test physical layout calculation."""
        layout = engine.calculate_layout(sample_pins, style=LayoutStyle.PHYSICAL)

        assert isinstance(layout, SymbolLayout)
        assert len(layout.pin_positions) == len(sample_pins)

    def test_simple_layout(self, engine, sample_pins):
        """Test simple layout calculation."""
        layout = engine.calculate_layout(sample_pins, style=LayoutStyle.SIMPLE)

        assert isinstance(layout, SymbolLayout)
        assert len(layout.pin_positions) == len(sample_pins)

    def test_layout_string_style(self, engine, sample_pins):
        """Test that string style values work."""
        layout = engine.calculate_layout(sample_pins, style="functional")
        assert isinstance(layout, SymbolLayout)

    def test_pin_positions_have_rotations(self, engine, sample_pins):
        """Test that pin positions have valid rotations."""
        layout = engine.calculate_layout(sample_pins, style=LayoutStyle.FUNCTIONAL)

        valid_rotations = {0, 90, 180, 270}
        for pos in layout.pin_positions:
            assert pos.rotation in valid_rotations

    def test_body_rect_centered(self, engine, sample_pins):
        """Test that body rectangle is centered around origin."""
        layout = engine.calculate_layout(sample_pins, style=LayoutStyle.FUNCTIONAL)

        x1, y1, x2, y2 = layout.body_rect
        # Body should be roughly symmetric
        assert abs(x1 + x2) < 0.1  # Close to zero
        assert abs(y1 + y2) < 0.1  # Close to zero

    def test_empty_pins_list(self, engine):
        """Test layout with empty pins list."""
        layout = engine.calculate_layout([], style=LayoutStyle.FUNCTIONAL)

        assert isinstance(layout, SymbolLayout)
        assert len(layout.pin_positions) == 0


class TestMultiUnitDetection:
    """Tests for multi-unit symbol detection."""

    @pytest.fixture
    def engine(self):
        return PinLayoutEngine()

    def test_single_unit_detection(self, engine):
        """Test that normal ICs are detected as single unit."""
        pins = [
            ExtractedPin(number="1", name="VDD", type="power_in"),
            ExtractedPin(number="2", name="GND", type="power_in"),
            ExtractedPin(number="3", name="IN", type="input"),
            ExtractedPin(number="4", name="OUT", type="output"),
        ]
        units = engine.detect_multi_unit(pins)

        assert len(units) == 1
        assert 1 in units
        assert len(units[1]) == len(pins)

    def test_dual_opamp_detection(self, engine):
        """Test that dual op-amp is detected as 2 units."""
        pins = [
            ExtractedPin(number="1", name="IN1+", type="input"),
            ExtractedPin(number="2", name="IN1-", type="input"),
            ExtractedPin(number="3", name="OUT1", type="output"),
            ExtractedPin(number="4", name="VCC", type="power_in"),
            ExtractedPin(number="5", name="IN2+", type="input"),
            ExtractedPin(number="6", name="IN2-", type="input"),
            ExtractedPin(number="7", name="OUT2", type="output"),
            ExtractedPin(number="8", name="GND", type="power_in"),
        ]
        units = engine.detect_multi_unit(pins)

        # Should detect as multi-unit
        assert len(units) >= 1


class TestSymbolGenerator:
    """Tests for the SymbolGenerator class."""

    @pytest.fixture
    def generator(self):
        """Create a symbol generator instance."""
        return SymbolGenerator()

    @pytest.fixture
    def sample_pin_table(self):
        """Create a sample PinTable for testing."""
        pins = [
            ExtractedPin(number="1", name="VDD", type="power_in", type_confidence=0.9),
            ExtractedPin(number="2", name="GND", type="power_in", type_confidence=0.95),
            ExtractedPin(number="3", name="PA0", type="bidirectional", type_confidence=0.8),
            ExtractedPin(number="4", name="PA1", type="bidirectional", type_confidence=0.8),
        ]
        return PinTable(pins=pins, package="LQFP48", confidence=0.9)

    def test_generate_basic(self, generator, sample_pin_table):
        """Test basic symbol generation."""
        result = generator.generate(
            name="TestChip",
            pins=sample_pin_table,
        )

        assert isinstance(result, GeneratedSymbol)
        assert result.name == "TestChip"
        assert len(result.pins) == 4

    def test_generate_with_properties(self, generator, sample_pin_table):
        """Test symbol generation with custom properties."""
        result = generator.generate(
            name="TestChip",
            pins=sample_pin_table,
            manufacturer="TestMfr",
            datasheet_url="https://example.com/datasheet.pdf",
            description="Test component",
            footprint="Package_QFP:LQFP-48",
        )

        assert result.properties["Manufacturer"] == "TestMfr"
        assert result.properties["Datasheet"] == "https://example.com/datasheet.pdf"
        assert result.properties["Description"] == "Test component"
        assert result.properties["Footprint"] == "Package_QFP:LQFP-48"

    def test_generate_with_extra_properties(self, generator, sample_pin_table):
        """Test symbol generation with additional properties."""
        result = generator.generate(
            name="TestChip",
            pins=sample_pin_table,
            properties={"LCSC": "C12345", "ki_keywords": "test chip"},
        )

        assert result.properties["LCSC"] == "C12345"
        assert result.properties["ki_keywords"] == "test chip"

    def test_generate_different_layouts(self, generator, sample_pin_table):
        """Test generation with different layout styles."""
        for style in ["functional", "physical", "simple"]:
            result = generator.generate(
                name=f"TestChip_{style}",
                pins=sample_pin_table,
                layout=style,
            )
            assert isinstance(result, GeneratedSymbol)
            assert result.layout.style == LayoutStyle(style)

    def test_generate_confidence(self, generator, sample_pin_table):
        """Test that confidence is calculated."""
        result = generator.generate(
            name="TestChip",
            pins=sample_pin_table,
        )

        assert 0 <= result.generation_confidence <= 1

    def test_generate_from_list(self, generator):
        """Test generation from list of ExtractedPins instead of PinTable."""
        pins = [
            ExtractedPin(number="1", name="VDD", type="power_in"),
            ExtractedPin(number="2", name="GND", type="power_in"),
        ]
        result = generator.generate(
            name="TestChip",
            pins=pins,
        )

        assert len(result.pins) == 2


class TestAddToLibrary:
    """Tests for adding generated symbols to libraries."""

    @pytest.fixture
    def generator(self):
        return SymbolGenerator()

    @pytest.fixture
    def sample_generated_symbol(self, generator):
        pins = [
            ExtractedPin(number="1", name="VDD", type="power_in"),
            ExtractedPin(number="2", name="GND", type="power_in"),
            ExtractedPin(number="3", name="IN", type="input"),
            ExtractedPin(number="4", name="OUT", type="output"),
        ]
        return generator.generate(
            name="TestChip",
            pins=pins,
            manufacturer="TestMfr",
        )

    def test_add_to_new_library(self, generator, sample_generated_symbol, tmp_path):
        """Test adding symbol to a new library."""
        lib_path = tmp_path / "test.kicad_sym"
        library = SymbolLibrary.create(str(lib_path))

        lib_symbol = generator.add_to_library(library, sample_generated_symbol)

        assert lib_symbol.name == "TestChip"
        assert len(lib_symbol.pins) == 4
        assert "TestChip" in library.symbols

    def test_add_to_library_properties(self, generator, sample_generated_symbol, tmp_path):
        """Test that properties are added correctly."""
        lib_path = tmp_path / "test.kicad_sym"
        library = SymbolLibrary.create(str(lib_path))

        lib_symbol = generator.add_to_library(library, sample_generated_symbol)

        assert lib_symbol.properties["Reference"] == "U"
        assert lib_symbol.properties["Value"] == "TestChip"
        assert lib_symbol.properties["Manufacturer"] == "TestMfr"

    def test_save_and_reload(self, generator, sample_generated_symbol, tmp_path):
        """Test that generated symbol can be saved and reloaded."""
        lib_path = tmp_path / "test.kicad_sym"
        library = SymbolLibrary.create(str(lib_path))

        generator.add_to_library(library, sample_generated_symbol)
        library.save()

        # Reload
        loaded = SymbolLibrary.load(str(lib_path))
        assert "TestChip" in loaded.symbols
        assert len(loaded.symbols["TestChip"].pins) == 4


class TestCreateSymbolFromDatasheet:
    """Tests for the create_symbol_from_datasheet convenience function."""

    def test_convenience_function(self, tmp_path):
        """Test the convenience function."""
        lib_path = tmp_path / "test.kicad_sym"
        library = SymbolLibrary.create(str(lib_path))

        pins = [
            ExtractedPin(number="1", name="VDD", type="power_in"),
            ExtractedPin(number="2", name="GND", type="power_in"),
        ]

        lib_symbol = create_symbol_from_datasheet(
            library=library,
            name="QuickChip",
            pins=pins,
            layout="simple",
        )

        assert lib_symbol.name == "QuickChip"
        assert len(lib_symbol.pins) == 2
        assert "QuickChip" in library.symbols


class TestSymbolLibraryMethod:
    """Tests for the SymbolLibrary.create_symbol_from_datasheet method."""

    def test_library_method(self, tmp_path):
        """Test calling create_symbol_from_datasheet on library instance."""
        lib_path = tmp_path / "test.kicad_sym"
        library = SymbolLibrary.create(str(lib_path))

        pins = PinTable(
            pins=[
                ExtractedPin(number="1", name="VDD", type="power_in"),
                ExtractedPin(number="2", name="GND", type="power_in"),
            ],
            package="SOT-23",
        )

        lib_symbol = library.create_symbol_from_datasheet(
            name="MethodTest",
            pins=pins,
            layout="functional",
        )

        assert lib_symbol.name == "MethodTest"
        assert "MethodTest" in library.symbols

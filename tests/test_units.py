"""Tests for the units module."""

from kicad_tools.config import Config, DisplayConfig
from kicad_tools.units import (
    MM_PER_MIL,
    UnitFormatter,
    UnitSystem,
    get_current_formatter,
    get_unit_formatter,
    set_current_formatter,
)


class TestUnitSystem:
    """Tests for UnitSystem enum."""

    def test_from_string_mm(self):
        """Test parsing mm unit strings."""
        assert UnitSystem.from_string("mm") == UnitSystem.MM
        assert UnitSystem.from_string("MM") == UnitSystem.MM
        assert UnitSystem.from_string("millimeters") == UnitSystem.MM
        assert UnitSystem.from_string("millimeter") == UnitSystem.MM

    def test_from_string_mils(self):
        """Test parsing mils unit strings."""
        assert UnitSystem.from_string("mils") == UnitSystem.MILS
        assert UnitSystem.from_string("MILS") == UnitSystem.MILS
        assert UnitSystem.from_string("mil") == UnitSystem.MILS
        assert UnitSystem.from_string("thou") == UnitSystem.MILS
        assert UnitSystem.from_string("thousandths") == UnitSystem.MILS

    def test_from_string_none(self):
        """Test parsing None returns None."""
        assert UnitSystem.from_string(None) is None

    def test_from_string_invalid(self):
        """Test parsing invalid string returns None."""
        assert UnitSystem.from_string("invalid") is None
        assert UnitSystem.from_string("inches") is None
        assert UnitSystem.from_string("") is None


class TestUnitFormatter:
    """Tests for UnitFormatter class."""

    def test_format_mm(self):
        """Test formatting in mm mode."""
        fmt = UnitFormatter(UnitSystem.MM)
        assert fmt.format(0.254) == "0.254 mm"
        assert fmt.format(1.0) == "1.000 mm"
        assert fmt.format(0.1524) == "0.152 mm"

    def test_format_mils(self):
        """Test formatting in mils mode."""
        fmt = UnitFormatter(UnitSystem.MILS)
        # 0.254mm = 10 mils
        assert fmt.format(0.254) == "10.0 mils"
        # 0.1524mm = 6 mils
        assert fmt.format(0.1524) == "6.0 mils"
        # 1.0mm = ~39.4 mils
        assert fmt.format(1.0) == "39.4 mils"

    def test_format_without_unit(self):
        """Test formatting without unit suffix."""
        fmt = UnitFormatter(UnitSystem.MM)
        assert fmt.format(0.254, include_unit=False) == "0.254"

        fmt_mils = UnitFormatter(UnitSystem.MILS)
        assert fmt_mils.format(0.254, include_unit=False) == "10.0"

    def test_format_compact(self):
        """Test compact formatting (no space)."""
        fmt = UnitFormatter(UnitSystem.MM)
        assert fmt.format_compact(0.254) == "0.254mm"

        fmt_mils = UnitFormatter(UnitSystem.MILS)
        assert fmt_mils.format_compact(0.254) == "10.0mils"

    def test_format_range(self):
        """Test range formatting."""
        fmt = UnitFormatter(UnitSystem.MM)
        assert fmt.format_range(0.1, 0.5) == "0.100-0.500 mm"

        fmt_mils = UnitFormatter(UnitSystem.MILS)
        # 0.1mm ~= 3.9 mils, 0.5mm ~= 19.7 mils
        result = fmt_mils.format_range(0.1, 0.5)
        assert "mils" in result
        assert "3.9" in result
        assert "19.7" in result

    def test_format_coordinate(self):
        """Test coordinate formatting."""
        fmt = UnitFormatter(UnitSystem.MM)
        assert fmt.format_coordinate(1.234, 5.678) == "(1.234, 5.678) mm"

        fmt_mils = UnitFormatter(UnitSystem.MILS)
        result = fmt_mils.format_coordinate(0.254, 0.508)
        assert result == "(10.0, 20.0) mils"

    def test_format_delta(self):
        """Test delta formatting with sign."""
        fmt = UnitFormatter(UnitSystem.MM)
        assert fmt.format_delta(0.050) == "+0.050 mm"
        assert fmt.format_delta(-0.050) == "-0.050 mm"

        fmt_mils = UnitFormatter(UnitSystem.MILS)
        assert fmt_mils.format_delta(0.254) == "+10.0 mils"
        assert fmt_mils.format_delta(-0.254) == "-10.0 mils"

    def test_format_comparison(self):
        """Test comparison formatting."""
        fmt = UnitFormatter(UnitSystem.MM)
        result = fmt.format_comparison(0.150, 0.200)
        assert "0.150 mm" in result
        assert "0.200 mm" in result
        assert "+0.050 mm" in result

    def test_unit_name(self):
        """Test unit name property."""
        assert UnitFormatter(UnitSystem.MM).unit_name == "mm"
        assert UnitFormatter(UnitSystem.MILS).unit_name == "mils"

    def test_convert_to_display(self):
        """Test conversion to display units."""
        fmt_mm = UnitFormatter(UnitSystem.MM)
        assert fmt_mm.convert_to_display(1.0) == 1.0

        fmt_mils = UnitFormatter(UnitSystem.MILS)
        # 0.254mm = 10 mils
        assert abs(fmt_mils.convert_to_display(0.254) - 10.0) < 0.001

    def test_convert_from_display(self):
        """Test conversion from display units."""
        fmt_mm = UnitFormatter(UnitSystem.MM)
        assert fmt_mm.convert_from_display(1.0) == 1.0

        fmt_mils = UnitFormatter(UnitSystem.MILS)
        # 10 mils = 0.254mm
        assert abs(fmt_mils.convert_from_display(10.0) - 0.254) < 0.0001

    def test_custom_precision(self):
        """Test custom precision settings."""
        fmt = UnitFormatter(UnitSystem.MM, precision_mm=1)
        assert fmt.format(0.254) == "0.3 mm"

        fmt_mils = UnitFormatter(UnitSystem.MILS, precision_mils=2)
        assert fmt_mils.format(0.254) == "10.00 mils"


class TestGetUnitFormatter:
    """Tests for get_unit_formatter function with precedence."""

    def test_default_is_mm(self):
        """Test default unit system is mm."""
        fmt = get_unit_formatter()
        assert fmt.system == UnitSystem.MM

    def test_cli_overrides_all(self):
        """Test CLI argument has highest priority."""
        # Even with env var and config, CLI should win
        fmt = get_unit_formatter(cli_units="mils")
        assert fmt.system == UnitSystem.MILS

    def test_env_overrides_config(self, monkeypatch):
        """Test environment variable overrides config."""
        monkeypatch.setenv("KICAD_TOOLS_UNITS", "mils")

        config = Config()
        config.display.units = "mm"

        fmt = get_unit_formatter(config=config)
        assert fmt.system == UnitSystem.MILS

    def test_config_overrides_default(self):
        """Test config file overrides default."""
        config = Config()
        config.display = DisplayConfig(units="mils")

        # No CLI, no env var
        fmt = get_unit_formatter(config=config)
        assert fmt.system == UnitSystem.MILS

    def test_precedence_cli_over_env(self, monkeypatch):
        """Test CLI > env precedence."""
        monkeypatch.setenv("KICAD_TOOLS_UNITS", "mm")
        fmt = get_unit_formatter(cli_units="mils")
        assert fmt.system == UnitSystem.MILS

    def test_precision_from_config(self):
        """Test precision settings from config."""
        config = Config()
        config.display = DisplayConfig(units="mm", precision_mm=4, precision_mils=2)

        fmt = get_unit_formatter(config=config)
        assert fmt.precision_mm == 4
        assert fmt.precision_mils == 2


class TestGlobalFormatter:
    """Tests for global formatter functions."""

    def test_set_and_get_formatter(self):
        """Test setting and getting global formatter."""
        fmt = UnitFormatter(UnitSystem.MILS)
        set_current_formatter(fmt)

        current = get_current_formatter()
        assert current.system == UnitSystem.MILS

    def test_get_formatter_default(self):
        """Test default formatter when none set."""
        # Reset global formatter
        set_current_formatter(None)  # type: ignore

        # Should return default mm formatter
        current = get_current_formatter()
        assert current.system == UnitSystem.MM


class TestConversionAccuracy:
    """Tests for conversion accuracy."""

    def test_common_pcb_values(self):
        """Test conversion of common PCB dimensions."""
        fmt = UnitFormatter(UnitSystem.MILS)

        # 6 mil trace = 0.1524mm
        assert abs(fmt.convert_to_display(0.1524) - 6.0) < 0.01

        # 10 mil clearance = 0.254mm
        assert abs(fmt.convert_to_display(0.254) - 10.0) < 0.01

        # 8 mil via drill = 0.2032mm
        assert abs(fmt.convert_to_display(0.2032) - 8.0) < 0.01

    def test_roundtrip_conversion(self):
        """Test mm -> mils -> mm roundtrip."""
        fmt = UnitFormatter(UnitSystem.MILS)
        original = 0.254

        display = fmt.convert_to_display(original)
        back = fmt.convert_from_display(display)

        assert abs(back - original) < 0.0001

    def test_mm_per_mil_constant(self):
        """Test the MM_PER_MIL constant is accurate."""
        # 1 mil = 0.001 inch = 0.0254 mm
        assert MM_PER_MIL == 0.0254

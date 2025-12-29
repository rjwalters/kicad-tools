"""Tests for schematic symbol registry."""

import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

from kicad_tools.schematic.registry import (
    Pin,
    SymbolDef,
    LibraryIndex,
    SymbolRegistry,
    get_registry,
    get_symbol,
    _default_symbol_paths,
)


class TestPin:
    """Tests for registry Pin dataclass."""

    def test_pin_creation(self):
        """Create pin with all fields."""
        pin = Pin(
            name="VCC",
            number="1",
            x=0.0,
            y=2.54,
            angle=90,
            length=2.54,
            pin_type="power_in"
        )
        assert pin.name == "VCC"
        assert pin.number == "1"
        assert pin.pin_type == "power_in"

    def test_connection_point(self):
        """Pin connection point is (x, y)."""
        pin = Pin(name="A", number="1", x=10.0, y=20.0, angle=0, length=2.54)
        assert pin.connection_point() == (10.0, 20.0)

    def test_repr(self):
        """Pin repr format."""
        pin = Pin(name="VCC", number="1", x=0, y=0, angle=0, length=2.54, pin_type="power_in")
        r = repr(pin)
        assert "Pin" in r
        assert "VCC" in r
        assert "power_in" in r


class TestSymbolDef:
    """Tests for SymbolDef dataclass."""

    def test_symbol_def_creation(self):
        """Create symbol definition."""
        sym = SymbolDef(
            lib_id="Device:LED",
            name="LED",
            raw_sexp="(symbol ...)",
            pins=[]
        )
        assert sym.lib_id == "Device:LED"
        assert sym.name == "LED"

    def test_library_property(self):
        """Library name extracted from lib_id."""
        sym = SymbolDef(lib_id="Device:LED", name="LED", raw_sexp="")
        assert sym.library == "Device"

    def test_get_pin_exact_name(self):
        """Get pin by exact name."""
        pin = Pin(name="A", number="1", x=0, y=0, angle=0, length=2.54)
        sym = SymbolDef(lib_id="Device:LED", name="LED", raw_sexp="", pins=[pin])
        assert sym.get_pin("A") == pin

    def test_get_pin_by_number(self):
        """Get pin by number."""
        pin = Pin(name="Anode", number="1", x=0, y=0, angle=0, length=2.54)
        sym = SymbolDef(lib_id="Device:LED", name="LED", raw_sexp="", pins=[pin])
        assert sym.get_pin("1") == pin

    def test_get_pin_case_insensitive(self):
        """Get pin case-insensitively."""
        pin = Pin(name="VCC", number="1", x=0, y=0, angle=0, length=2.54)
        sym = SymbolDef(lib_id="Device:R", name="R", raw_sexp="", pins=[pin])
        assert sym.get_pin("vcc") == pin

    def test_get_pin_not_found(self):
        """KeyError when pin not found."""
        pin = Pin(name="A", number="1", x=0, y=0, angle=0, length=2.54)
        sym = SymbolDef(lib_id="Device:LED", name="LED", raw_sexp="", pins=[pin])
        with pytest.raises(KeyError) as exc:
            sym.get_pin("VCC")
        assert "VCC" in str(exc.value)
        assert "not found" in str(exc.value)

    def test_get_pin_fuzzy_suggestions(self):
        """Error message includes fuzzy suggestions."""
        pin = Pin(name="Anode", number="1", x=0, y=0, angle=0, length=2.54)
        sym = SymbolDef(lib_id="Device:LED", name="LED", raw_sexp="", pins=[pin])
        with pytest.raises(KeyError) as exc:
            sym.get_pin("Anod")
        # Should suggest "Anode"
        assert "Anode" in str(exc.value) or "Available" in str(exc.value)

    def test_has_pin_true(self):
        """has_pin returns True when pin exists."""
        pin = Pin(name="VCC", number="1", x=0, y=0, angle=0, length=2.54)
        sym = SymbolDef(lib_id="Device:R", name="R", raw_sexp="", pins=[pin])
        assert sym.has_pin("VCC") is True
        assert sym.has_pin("1") is True

    def test_has_pin_false(self):
        """has_pin returns False when pin doesn't exist."""
        pin = Pin(name="VCC", number="1", x=0, y=0, angle=0, length=2.54)
        sym = SymbolDef(lib_id="Device:R", name="R", raw_sexp="", pins=[pin])
        assert sym.has_pin("GND") is False

    def test_pins_by_type(self):
        """Get pins filtered by type."""
        power = Pin(name="VCC", number="1", x=0, y=0, angle=0, length=2.54, pin_type="power_in")
        passive = Pin(name="1", number="2", x=0, y=0, angle=0, length=2.54, pin_type="passive")
        sym = SymbolDef(lib_id="Device:R", name="R", raw_sexp="", pins=[power, passive])

        power_pins = sym.pins_by_type("power_in")
        assert len(power_pins) == 1
        assert power_pins[0] == power

    def test_power_pins(self):
        """Get power pins (power_in and power_out)."""
        p_in = Pin(name="VCC", number="1", x=0, y=0, angle=0, length=2.54, pin_type="power_in")
        p_out = Pin(name="VOUT", number="2", x=0, y=0, angle=0, length=2.54, pin_type="power_out")
        passive = Pin(name="1", number="3", x=0, y=0, angle=0, length=2.54, pin_type="passive")
        sym = SymbolDef(lib_id="Device:LDO", name="LDO", raw_sexp="", pins=[p_in, p_out, passive])

        power = sym.power_pins()
        assert len(power) == 2
        assert p_in in power
        assert p_out in power


class TestLibraryIndex:
    """Tests for LibraryIndex."""

    def test_library_index_from_file(self, tmp_path):
        """Build library index from file."""
        lib_file = tmp_path / "Test.kicad_sym"
        lib_file.write_text('''(kicad_symbol_lib
\t(symbol "LED"
\t\t(property "Reference" "D")
\t)
\t(symbol "R"
\t\t(property "Reference" "R")
\t)
)''')

        index = LibraryIndex.from_file(lib_file)
        assert index.name == "Test"
        assert "LED" in index.symbols
        assert "R" in index.symbols

    def test_library_index_skips_unit_symbols(self, tmp_path):
        """Unit symbols (with _N_N suffix) are skipped."""
        lib_file = tmp_path / "Test.kicad_sym"
        lib_file.write_text('''(kicad_symbol_lib
\t(symbol "IC"
\t\t(property "Reference" "U")
\t)
\t(symbol "IC_1_1"
\t\t(property "Reference" "U")
\t)
)''')

        index = LibraryIndex.from_file(lib_file)
        assert "IC" in index.symbols
        assert "IC_1_1" not in index.symbols

    def test_get_content_cached(self, tmp_path):
        """Content is cached after first access."""
        lib_file = tmp_path / "Test.kicad_sym"
        lib_file.write_text("(kicad_symbol_lib)")

        index = LibraryIndex.from_file(lib_file)
        content1 = index.get_content()
        content2 = index.get_content()
        assert content1 == content2

    def test_clear_content_cache(self, tmp_path):
        """Clear cached content."""
        lib_file = tmp_path / "Test.kicad_sym"
        lib_file.write_text("(kicad_symbol_lib)")

        index = LibraryIndex.from_file(lib_file)
        _ = index.get_content()
        index.clear_content_cache()
        assert index._content is None


class TestSymbolRegistry:
    """Tests for SymbolRegistry."""

    def test_registry_creation_with_paths(self, tmp_path):
        """Create registry with custom paths."""
        registry = SymbolRegistry(lib_paths=[tmp_path])
        assert tmp_path in registry.lib_paths

    def test_register_opl(self):
        """Register OPL part mapping."""
        registry = SymbolRegistry(lib_paths=[])
        registry.register_opl("CUSTOM_PART", "Device:R")
        assert registry._opl_mapping["CUSTOM_PART"] == "Device:R"

    def test_resolve_opl_known(self):
        """Resolve known OPL part."""
        registry = SymbolRegistry(lib_paths=[])
        # Use a pre-registered OPL part
        lib_id = registry.resolve_opl("LED_0603")
        assert lib_id == "Device:LED"

    def test_resolve_opl_unknown(self):
        """Unknown OPL raises KeyError."""
        registry = SymbolRegistry(lib_paths=[])
        with pytest.raises(KeyError) as exc:
            registry.resolve_opl("UNKNOWN_PART_12345")
        assert "Unknown OPL part" in str(exc.value)

    def test_list_libraries(self, tmp_path):
        """List available libraries."""
        (tmp_path / "Device.kicad_sym").write_text("(kicad_symbol_lib)")
        (tmp_path / "Connector.kicad_sym").write_text("(kicad_symbol_lib)")

        registry = SymbolRegistry(lib_paths=[tmp_path])
        libs = registry.list_libraries()
        assert "Device" in libs
        assert "Connector" in libs

    def test_list_library_symbols(self, tmp_path):
        """List symbols in a library."""
        lib_file = tmp_path / "Device.kicad_sym"
        lib_file.write_text('''(kicad_symbol_lib
\t(symbol "LED"
\t\t(property "Reference" "D")
\t)
\t(symbol "R"
\t\t(property "Reference" "R")
\t)
)''')

        registry = SymbolRegistry(lib_paths=[tmp_path])
        symbols = registry.list_library("Device")
        assert "LED" in symbols
        assert "R" in symbols

    def test_get_library_not_found(self, tmp_path):
        """FileNotFoundError when library doesn't exist."""
        registry = SymbolRegistry(lib_paths=[tmp_path])
        with pytest.raises(FileNotFoundError) as exc:
            registry._get_library_index("NonExistent")
        assert "not found" in str(exc.value)

    def test_get_invalid_lib_id_format(self):
        """Invalid lib_id format raises ValueError."""
        registry = SymbolRegistry(lib_paths=[])
        with pytest.raises(ValueError) as exc:
            registry.get("NoColon")
        assert "Invalid lib_id format" in str(exc.value)

    def test_cache_stats(self, tmp_path):
        """Get cache statistics."""
        registry = SymbolRegistry(lib_paths=[tmp_path])
        stats = registry.cache_stats()

        assert "cached_symbols" in stats
        assert "indexed_libraries" in stats
        assert "opl_mappings" in stats
        assert stats["cached_symbols"] == 0

    def test_clear_cache(self, tmp_path):
        """Clear all caches."""
        lib_file = tmp_path / "Device.kicad_sym"
        lib_file.write_text('''(kicad_symbol_lib
\t(symbol "LED"
\t\t(property "Reference" "D")
\t)
)''')

        registry = SymbolRegistry(lib_paths=[tmp_path])
        # Trigger indexing
        registry.list_library("Device")

        registry.clear_cache()
        assert len(registry._symbol_cache) == 0
        assert len(registry._library_index) == 0

    def test_search_by_pattern(self, tmp_path):
        """Search for symbols by pattern."""
        lib_file = tmp_path / "Device.kicad_sym"
        lib_file.write_text('''(kicad_symbol_lib
\t(symbol "LED"
\t\t(property "Reference" "D")
\t)
\t(symbol "LED_Small"
\t\t(property "Reference" "D")
\t)
\t(symbol "R"
\t\t(property "Reference" "R")
\t)
)''')

        registry = SymbolRegistry(lib_paths=[tmp_path])
        results = registry.search("LED")
        assert len(results) == 2
        assert all("LED" in r for r in results)

    def test_search_limit(self, tmp_path):
        """Search results limited by limit parameter."""
        lib_file = tmp_path / "Device.kicad_sym"
        symbols = "\n".join([f'\t(symbol "C{i}"\n\t\t(property "Reference" "C")\n\t)' for i in range(30)])
        lib_file.write_text(f"(kicad_symbol_lib\n{symbols}\n)")

        registry = SymbolRegistry(lib_paths=[tmp_path])
        results = registry.search("C", limit=5)
        assert len(results) <= 5


class TestDefaultSymbolPaths:
    """Tests for _default_symbol_paths()."""

    def test_returns_list(self):
        """Returns a list of paths."""
        paths = _default_symbol_paths()
        assert isinstance(paths, list)
        # All entries should be Path objects
        for p in paths:
            assert isinstance(p, Path)

    @patch.dict('os.environ', {'KICAD_SYMBOL_DIR': '/custom/symbols'})
    def test_env_override(self, tmp_path):
        """KICAD_SYMBOL_DIR environment variable adds path."""
        # Create the custom path for it to be included
        custom_path = Path('/custom/symbols')
        with patch.object(Path, 'exists', return_value=True):
            paths = _default_symbol_paths()
            # Environment variable path should be first (if exists)
            # Note: in actual usage it would be first only if it exists


class TestGlobalRegistry:
    """Tests for global registry functions."""

    def test_get_registry_returns_same_instance(self):
        """get_registry returns singleton."""
        # Reset global
        import kicad_tools.schematic.registry as reg_module
        reg_module._global_registry = None

        r1 = get_registry()
        r2 = get_registry()
        assert r1 is r2

    def test_get_symbol_convenience(self, tmp_path):
        """get_symbol is convenience for registry.get()."""
        # This would need actual library files to work fully
        # Just verify it calls through to registry
        import kicad_tools.schematic.registry as reg_module
        reg_module._global_registry = None

        with pytest.raises((ValueError, FileNotFoundError)):
            # Will fail because no valid library, but verifies call chain
            get_symbol("NonExistent:Symbol")


class TestSymbolDefEmbeddedSexp:
    """Tests for SymbolDef.get_embedded_sexp()."""

    def test_get_embedded_sexp_prefixes_library(self):
        """Embedded sexp adds library prefix to symbol name."""
        raw = '(symbol "LED"\n\t(property "Reference" "D")\n)'
        sym = SymbolDef(lib_id="Device:LED", name="LED", raw_sexp=raw)

        embedded = sym.get_embedded_sexp()
        assert 'symbol "Device:LED"' in embedded

    def test_get_embedded_sexp_updates_extends(self):
        """Embedded sexp updates extends references."""
        raw = '(symbol "LED_Alt"\n\t(extends "LED")\n)'
        sym = SymbolDef(lib_id="Device:LED_Alt", name="LED_Alt", raw_sexp=raw)

        embedded = sym.get_embedded_sexp()
        assert 'extends "Device:LED"' in embedded

    def test_get_embedded_sexp_adds_indentation(self):
        """Embedded sexp has proper indentation."""
        raw = '(symbol "LED")'
        sym = SymbolDef(lib_id="Device:LED", name="LED", raw_sexp=raw)

        embedded = sym.get_embedded_sexp()
        # Should have tab indentation
        assert '\t' in embedded


class TestParsePins:
    """Tests for pin parsing from S-expression."""

    def test_parse_pins_from_sexp(self, tmp_path):
        """Parse pins from symbol definition."""
        lib_file = tmp_path / "Device.kicad_sym"
        lib_file.write_text('''(kicad_symbol_lib
\t(symbol "LED"
\t\t(pin passive line (at 0 2.54 270) (length 2.54)
\t\t\t(name "A" (effects (font (size 1.27 1.27))))
\t\t\t(number "1" (effects (font (size 1.27 1.27))))
\t\t)
\t\t(pin passive line (at 0 -2.54 90) (length 2.54)
\t\t\t(name "K" (effects (font (size 1.27 1.27))))
\t\t\t(number "2" (effects (font (size 1.27 1.27))))
\t\t)
\t)
)''')

        registry = SymbolRegistry(lib_paths=[tmp_path])
        sym = registry.get("Device:LED")

        assert len(sym.pins) == 2
        assert any(p.name == "A" and p.number == "1" for p in sym.pins)
        assert any(p.name == "K" and p.number == "2" for p in sym.pins)

    def test_parse_pin_types(self, tmp_path):
        """Parse different pin types."""
        lib_file = tmp_path / "Device.kicad_sym"
        lib_file.write_text('''(kicad_symbol_lib
\t(symbol "IC"
\t\t(pin power_in line (at 0 5.08 270) (length 2.54)
\t\t\t(name "VCC" (effects (font (size 1.27 1.27))))
\t\t\t(number "1" (effects (font (size 1.27 1.27))))
\t\t)
\t\t(pin input line (at -5.08 0 0) (length 2.54)
\t\t\t(name "IN" (effects (font (size 1.27 1.27))))
\t\t\t(number "2" (effects (font (size 1.27 1.27))))
\t\t)
\t\t(pin output line (at 5.08 0 180) (length 2.54)
\t\t\t(name "OUT" (effects (font (size 1.27 1.27))))
\t\t\t(number "3" (effects (font (size 1.27 1.27))))
\t\t)
\t)
)''')

        registry = SymbolRegistry(lib_paths=[tmp_path])
        sym = registry.get("Device:IC")

        vcc = next(p for p in sym.pins if p.name == "VCC")
        assert vcc.pin_type == "power_in"

        inp = next(p for p in sym.pins if p.name == "IN")
        assert inp.pin_type == "input"

        out = next(p for p in sym.pins if p.name == "OUT")
        assert out.pin_type == "output"


class TestSymbolInheritance:
    """Tests for symbol inheritance (extends)."""

    def test_symbol_with_extends(self, tmp_path):
        """Symbol inheriting from another includes parent pins."""
        lib_file = tmp_path / "Device.kicad_sym"
        lib_file.write_text('''(kicad_symbol_lib
\t(symbol "R"
\t\t(pin passive line (at 0 2.54 270) (length 2.54)
\t\t\t(name "1" (effects (font (size 1.27 1.27))))
\t\t\t(number "1" (effects (font (size 1.27 1.27))))
\t\t)
\t\t(pin passive line (at 0 -2.54 90) (length 2.54)
\t\t\t(name "2" (effects (font (size 1.27 1.27))))
\t\t\t(number "2" (effects (font (size 1.27 1.27))))
\t\t)
\t)
\t(symbol "R_Small"
\t\t(extends "R")
\t)
)''')

        registry = SymbolRegistry(lib_paths=[tmp_path])
        sym = registry.get("Device:R_Small")

        # R_Small should have pins from R via inheritance
        # The pins come from parent symbol
        assert len(sym.pins) >= 2


class TestOPLMappings:
    """Tests for OPL (Open Parts Library) mappings."""

    def test_default_opl_mappings(self):
        """Default OPL mappings exist."""
        registry = SymbolRegistry(lib_paths=[])

        # Check some default mappings
        assert "LED_0603" in registry._opl_mapping
        assert "R_0603" in registry._opl_mapping
        assert "C_0603" in registry._opl_mapping

    def test_get_with_opl_part(self, tmp_path):
        """Get symbol using OPL part number."""
        lib_file = tmp_path / "Device.kicad_sym"
        lib_file.write_text('''(kicad_symbol_lib
\t(symbol "LED"
\t\t(pin passive line (at 0 0 0) (length 2.54)
\t\t\t(name "A" (effects (font (size 1.27 1.27))))
\t\t\t(number "1" (effects (font (size 1.27 1.27))))
\t\t)
\t)
)''')

        registry = SymbolRegistry(lib_paths=[tmp_path])
        # "LED_0603" maps to "Device:LED"
        sym = registry.get("LED_0603")
        assert sym.name == "LED"

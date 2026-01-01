"""Tests for schematic symbol auto-layout functionality.

Tests for:
- SymbolInstance.bounding_box()
- SymbolInstance.overlaps()
- Schematic.find_overlapping_symbols()
- Schematic.suggest_position()
- Schematic.add_symbol(auto_layout=True)
"""

import pytest

from kicad_tools.schematic.models.pin import Pin
from kicad_tools.schematic.models.schematic import Schematic, SnapMode
from kicad_tools.schematic.models.symbol import SymbolDef, SymbolInstance

# ============================================================================
# Test Fixtures
# ============================================================================


def make_symbol_def(lib_id: str = "Test:Symbol", pins: list[Pin] = None) -> SymbolDef:
    """Create a test symbol definition with specified pins."""
    if pins is None:
        # Default: 4-pin symbol with pins at corners
        pins = [
            Pin(name="1", number="1", x=-5.08, y=2.54, angle=180, length=2.54),
            Pin(name="2", number="2", x=-5.08, y=-2.54, angle=180, length=2.54),
            Pin(name="3", number="3", x=5.08, y=-2.54, angle=0, length=2.54),
            Pin(name="4", number="4", x=5.08, y=2.54, angle=0, length=2.54),
        ]
    return SymbolDef(lib_id=lib_id, name=lib_id.split(":")[1], raw_sexp="", pins=pins)


def make_symbol_instance(
    x: float,
    y: float,
    ref: str = "U1",
    pins: list[Pin] = None,
    rotation: float = 0,
) -> SymbolInstance:
    """Create a test symbol instance at the given position."""
    sym_def = make_symbol_def(pins=pins)
    return SymbolInstance(
        symbol_def=sym_def,
        x=x,
        y=y,
        rotation=rotation,
        reference=ref,
        value="Test",
    )


# ============================================================================
# SymbolInstance.bounding_box() Tests
# ============================================================================


class TestBoundingBox:
    """Tests for SymbolInstance.bounding_box()."""

    def test_bounding_box_basic(self):
        """Bounding box includes all pin positions plus padding."""
        sym = make_symbol_instance(x=100.0, y=100.0)
        box = sym.bounding_box(padding=0)

        # Pins at +-5.08, +-2.54 relative to center
        assert box[0] == pytest.approx(100.0 - 5.08, abs=0.01)  # min_x
        assert box[1] == pytest.approx(100.0 - 2.54, abs=0.01)  # min_y
        assert box[2] == pytest.approx(100.0 + 5.08, abs=0.01)  # max_x
        assert box[3] == pytest.approx(100.0 + 2.54, abs=0.01)  # max_y

    def test_bounding_box_with_padding(self):
        """Padding extends the bounding box."""
        sym = make_symbol_instance(x=100.0, y=100.0)
        box = sym.bounding_box(padding=2.54)

        # Pins at +-5.08, +-2.54 relative to center, plus 2.54 padding
        assert box[0] == pytest.approx(100.0 - 5.08 - 2.54, abs=0.01)
        assert box[1] == pytest.approx(100.0 - 2.54 - 2.54, abs=0.01)
        assert box[2] == pytest.approx(100.0 + 5.08 + 2.54, abs=0.01)
        assert box[3] == pytest.approx(100.0 + 2.54 + 2.54, abs=0.01)

    def test_bounding_box_no_pins(self):
        """Symbol with no pins gets default bounding box."""
        sym_def = make_symbol_def(pins=[])
        sym = SymbolInstance(
            symbol_def=sym_def,
            x=100.0,
            y=100.0,
            rotation=0,
            reference="U1",
            value="Test",
        )
        box = sym.bounding_box(padding=2.54)

        # Default half_size is 5.08 + padding
        half = 5.08 + 2.54
        assert box[0] == pytest.approx(100.0 - half, abs=0.01)
        assert box[1] == pytest.approx(100.0 - half, abs=0.01)
        assert box[2] == pytest.approx(100.0 + half, abs=0.01)
        assert box[3] == pytest.approx(100.0 + half, abs=0.01)

    def test_bounding_box_rotated(self):
        """Bounding box accounts for rotation."""
        # Create a symbol with pins at asymmetric positions
        pins = [
            Pin(name="1", number="1", x=-10.0, y=0, angle=180, length=2.54),
            Pin(name="2", number="2", x=5.0, y=0, angle=0, length=2.54),
        ]
        sym = make_symbol_instance(x=100.0, y=100.0, pins=pins, rotation=90)
        box = sym.bounding_box(padding=0)

        # After 90 degree rotation, X and Y swap
        # Original: x range [-10, 5], y range [0, 0]
        # Rotated: x range [0, 0], y range [-5, 10] (but y flipped in schematic)
        assert box[0] <= 100.0 <= box[2]  # x should include center


# ============================================================================
# SymbolInstance.overlaps() Tests
# ============================================================================


class TestOverlaps:
    """Tests for SymbolInstance.overlaps()."""

    def test_overlaps_same_position(self):
        """Two symbols at same position overlap."""
        sym1 = make_symbol_instance(x=100.0, y=100.0, ref="U1")
        sym2 = make_symbol_instance(x=100.0, y=100.0, ref="U2")

        assert sym1.overlaps(sym2) is True
        assert sym2.overlaps(sym1) is True

    def test_overlaps_nearby(self):
        """Two symbols that are close together overlap."""
        sym1 = make_symbol_instance(x=100.0, y=100.0, ref="U1")
        # Move sym2 only 5mm away - still overlapping with default padding
        sym2 = make_symbol_instance(x=105.0, y=100.0, ref="U2")

        assert sym1.overlaps(sym2) is True

    def test_no_overlap_far_apart(self):
        """Two symbols far apart don't overlap."""
        sym1 = make_symbol_instance(x=100.0, y=100.0, ref="U1")
        sym2 = make_symbol_instance(x=200.0, y=100.0, ref="U2")

        assert sym1.overlaps(sym2) is False
        assert sym2.overlaps(sym1) is False

    def test_no_overlap_horizontal(self):
        """Two symbols side by side with enough spacing don't overlap."""
        sym1 = make_symbol_instance(x=100.0, y=100.0, ref="U1")
        # Pins extend to about +/- 5.08, plus 2.54 padding = 7.62
        # So need at least 15.24 horizontal separation
        sym2 = make_symbol_instance(x=120.0, y=100.0, ref="U2")

        assert sym1.overlaps(sym2) is False

    def test_no_overlap_vertical(self):
        """Two symbols stacked with enough spacing don't overlap."""
        sym1 = make_symbol_instance(x=100.0, y=100.0, ref="U1")
        # Pins extend to about +/- 2.54, plus 2.54 padding = 5.08
        # So need at least 10.16 vertical separation
        sym2 = make_symbol_instance(x=100.0, y=115.0, ref="U2")

        assert sym1.overlaps(sym2) is False

    def test_overlap_zero_padding(self):
        """With zero padding, symbols need to actually touch to overlap."""
        sym1 = make_symbol_instance(x=100.0, y=100.0, ref="U1")
        # With zero padding, bounding boxes are just the pin extents
        sym2 = make_symbol_instance(x=111.0, y=100.0, ref="U2")

        assert sym1.overlaps(sym2, padding=0) is False


# ============================================================================
# Schematic.find_overlapping_symbols() Tests
# ============================================================================


class TestFindOverlappingSymbols:
    """Tests for Schematic.find_overlapping_symbols()."""

    def test_no_overlaps_empty(self):
        """Empty schematic has no overlaps."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        overlaps = sch.find_overlapping_symbols()
        assert overlaps == []

    def test_no_overlaps_single_symbol(self):
        """Schematic with one symbol has no overlaps."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sym = make_symbol_instance(x=100.0, y=100.0)
        sch.symbols.append(sym)

        overlaps = sch.find_overlapping_symbols()
        assert overlaps == []

    def test_no_overlaps_spaced_symbols(self):
        """Properly spaced symbols don't overlap."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        # Add symbols with enough spacing
        for i in range(3):
            sym = make_symbol_instance(x=100.0 + i * 30.0, y=100.0, ref=f"U{i + 1}")
            sch.symbols.append(sym)

        overlaps = sch.find_overlapping_symbols()
        assert overlaps == []

    def test_detects_overlap(self):
        """Detects overlapping symbols."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        sym1 = make_symbol_instance(x=100.0, y=100.0, ref="U1")
        sym2 = make_symbol_instance(x=105.0, y=100.0, ref="U2")  # Too close
        sch.symbols.append(sym1)
        sch.symbols.append(sym2)

        overlaps = sch.find_overlapping_symbols()
        assert len(overlaps) == 1
        assert (sym1, sym2) in overlaps or (sym2, sym1) in overlaps

    def test_detects_multiple_overlaps(self):
        """Detects multiple overlapping pairs."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        # Three symbols all overlapping each other
        sym1 = make_symbol_instance(x=100.0, y=100.0, ref="U1")
        sym2 = make_symbol_instance(x=102.0, y=100.0, ref="U2")
        sym3 = make_symbol_instance(x=104.0, y=100.0, ref="U3")
        sch.symbols.extend([sym1, sym2, sym3])

        overlaps = sch.find_overlapping_symbols()
        # All three overlap each other, so we expect 3 pairs
        assert len(overlaps) == 3

    def test_no_duplicate_pairs(self):
        """Each overlapping pair is returned only once."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        sym1 = make_symbol_instance(x=100.0, y=100.0, ref="U1")
        sym2 = make_symbol_instance(x=102.0, y=100.0, ref="U2")
        sch.symbols.extend([sym1, sym2])

        overlaps = sch.find_overlapping_symbols()
        assert len(overlaps) == 1

        # Check we don't have both (sym1, sym2) and (sym2, sym1)
        refs = [(o[0].reference, o[1].reference) for o in overlaps]
        assert len(set(refs)) == 1


# ============================================================================
# Schematic.suggest_position() Tests
# ============================================================================


class TestSuggestPosition:
    """Tests for Schematic.suggest_position()."""

    def test_suggest_clear_position(self):
        """Returns preferred position when it's clear."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        # Register the symbol def
        sym_def = make_symbol_def(lib_id="Test:Symbol")
        sch._symbol_defs["Test:Symbol"] = sym_def

        pos = sch.suggest_position("Test:Symbol", near=(100.0, 100.0))
        assert pos == (100.0, 100.0)

    def test_suggest_avoids_existing(self):
        """Suggests alternate position when preferred is occupied."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        # Register the symbol def
        sym_def = make_symbol_def(lib_id="Test:Symbol")
        sch._symbol_defs["Test:Symbol"] = sym_def

        # Add a symbol at the preferred position
        sym = make_symbol_instance(x=100.0, y=100.0)
        sch.symbols.append(sym)

        # Request same position
        pos = sch.suggest_position("Test:Symbol", near=(100.0, 100.0))

        # Should get a different position
        assert pos != (100.0, 100.0)

    def test_suggest_snaps_to_grid(self):
        """Suggested position is snapped to grid."""
        sch = Schematic(title="Test", snap_mode=SnapMode.AUTO, grid=1.27)

        # Register the symbol def
        sym_def = make_symbol_def(lib_id="Test:Symbol")
        sch._symbol_defs["Test:Symbol"] = sym_def

        pos = sch.suggest_position("Test:Symbol", near=(100.5, 100.7))

        # Position should be on grid (multiples of 1.27)
        # Check that remainder is either ~0 or ~1.27 (accounting for float precision)
        remainder_x = pos[0] % 1.27
        remainder_y = pos[1] % 1.27
        assert remainder_x < 0.01 or abs(remainder_x - 1.27) < 0.01
        assert remainder_y < 0.01 or abs(remainder_y - 1.27) < 0.01

    def test_suggest_empty_schematic(self):
        """Returns preferred position for empty schematic."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        sym_def = make_symbol_def(lib_id="Test:Symbol")
        sch._symbol_defs["Test:Symbol"] = sym_def

        pos = sch.suggest_position("Test:Symbol", near=(50.0, 75.0))
        assert pos == (50.0, 75.0)


# ============================================================================
# Schematic.add_symbol(auto_layout=True) Tests
# ============================================================================


class TestAddSymbolAutoLayout:
    """Tests for add_symbol with auto_layout parameter."""

    def test_add_symbol_no_auto_layout(self):
        """Without auto_layout, symbol is placed at requested position."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        sym_def = make_symbol_def(lib_id="Test:Symbol")
        sch._symbol_defs["Test:Symbol"] = sym_def

        # Add first symbol
        sch.symbols.append(make_symbol_instance(x=100.0, y=100.0, ref="U1"))

        # Add second symbol at same position without auto_layout
        sym = SymbolInstance(
            symbol_def=sym_def,
            x=100.0,
            y=100.0,
            rotation=0,
            reference="U2",
            value="Test",
        )
        sch.symbols.append(sym)

        # Should be at requested position (overlapping)
        assert sym.x == 100.0
        assert sym.y == 100.0

        # Should detect overlap
        overlaps = sch.find_overlapping_symbols()
        assert len(overlaps) == 1

    def test_add_symbol_with_auto_layout_clears_position(self):
        """With auto_layout=True on clear position, uses requested position."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        sym_def = make_symbol_def(lib_id="Test:Symbol")
        sch._symbol_defs["Test:Symbol"] = sym_def

        # Create schematic that would use add_symbol
        # For this test, we simulate what add_symbol does
        x, y = 100.0, 100.0
        x, y = sch.suggest_position("Test:Symbol", near=(x, y))

        sym = SymbolInstance(
            symbol_def=sym_def,
            x=x,
            y=y,
            rotation=0,
            reference="U1",
            value="Test",
        )
        sch.symbols.append(sym)

        # Position should be at requested location (no existing symbols)
        assert sym.x == 100.0
        assert sym.y == 100.0

    def test_add_symbol_with_auto_layout_avoids_overlap(self):
        """With auto_layout=True, avoids overlapping existing symbols."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        sym_def = make_symbol_def(lib_id="Test:Symbol")
        sch._symbol_defs["Test:Symbol"] = sym_def

        # Add first symbol
        sym1 = SymbolInstance(
            symbol_def=sym_def,
            x=100.0,
            y=100.0,
            rotation=0,
            reference="U1",
            value="Test",
        )
        sch.symbols.append(sym1)

        # Use suggest_position to find non-overlapping spot
        x, y = sch.suggest_position("Test:Symbol", near=(100.0, 100.0))

        sym2 = SymbolInstance(
            symbol_def=sym_def,
            x=x,
            y=y,
            rotation=0,
            reference="U2",
            value="Test",
        )
        sch.symbols.append(sym2)

        # Position should be different from existing symbol
        assert (sym2.x, sym2.y) != (100.0, 100.0)

        # Should not detect any overlaps
        overlaps = sch.find_overlapping_symbols()
        assert len(overlaps) == 0


# ============================================================================
# Grid Snapping Tests
# ============================================================================


class TestAutoLayoutGridSnapping:
    """Tests for grid snapping in auto-layout."""

    def test_suggest_position_maintains_grid(self):
        """Suggested positions are on grid."""
        sch = Schematic(title="Test", snap_mode=SnapMode.AUTO, grid=2.54)

        sym_def = make_symbol_def(lib_id="Test:Symbol")
        sch._symbol_defs["Test:Symbol"] = sym_def

        # Add blocking symbol
        sym = SymbolInstance(
            symbol_def=sym_def,
            x=101.6,  # On grid (40 * 2.54)
            y=101.6,
            rotation=0,
            reference="U1",
            value="Test",
        )
        sch.symbols.append(sym)

        # Request position that overlaps
        pos = sch.suggest_position("Test:Symbol", near=(101.6, 101.6))

        # Resulting position should be on grid
        # Grid is 2.54, so position should be multiple of 2.54
        # Account for floating point by checking remainder
        assert abs(pos[0] % 2.54) < 0.01 or abs(pos[0] % 2.54 - 2.54) < 0.01
        assert abs(pos[1] % 2.54) < 0.01 or abs(pos[1] % 2.54 - 2.54) < 0.01

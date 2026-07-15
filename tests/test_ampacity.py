"""Tests for IPC-2221 ampacity width derivation (physics/ampacity.py, #4216)."""

from __future__ import annotations

import pytest

from kicad_tools.physics import width_for_current as width_for_current_exported
from kicad_tools.physics.ampacity import width_for_current


class TestWidthForCurrentGoldenValues:
    """Golden numeric checks against curator-verified IPC-2221 results."""

    def test_golden_15a_2oz_external(self):
        """15 A / 2 oz / 10 C / external -> 6.29 mm (curator-verified golden)."""
        w = width_for_current(15, copper_weight_oz=2, delta_t_c=10, layer="external")
        assert w == pytest.approx(6.29, abs=0.05)

    def test_golden_15a_2oz_internal(self):
        """Internal layer (k=0.024) is much wider than external for the same inputs."""
        w = width_for_current(15, copper_weight_oz=2, delta_t_c=10, layer="internal")
        assert w == pytest.approx(16.37, abs=0.05)

    def test_internal_wider_than_external(self):
        """Sanity: internal layer always needs a wider trace (k=0.024 < 0.048)."""
        ext = width_for_current(15, copper_weight_oz=2, delta_t_c=10, layer="external")
        internal = width_for_current(15, copper_weight_oz=2, delta_t_c=10, layer="internal")
        assert internal > ext

    def test_golden_15a_1oz_external(self):
        """Thinner copper -> wider trace: 15 A / 1 oz / external ~= 12.585 mm."""
        w = width_for_current(15, copper_weight_oz=1, delta_t_c=10, layer="external")
        assert w == pytest.approx(12.585, abs=0.05)

    def test_golden_15a_half_oz_internal(self):
        """Thin inner copper on an internal layer is impractically wide (~65.48 mm)."""
        w = width_for_current(15, copper_weight_oz=0.5, delta_t_c=10, layer="internal")
        assert w == pytest.approx(65.48, abs=0.1)


class TestWidthForCurrentBehavior:
    """Monotonicity and default-argument behavior."""

    def test_default_delta_t_is_10c(self):
        """Omitting delta_t_c uses the documented 10 C default."""
        implicit = width_for_current(15, copper_weight_oz=2, layer="external")
        explicit = width_for_current(15, copper_weight_oz=2, delta_t_c=10.0, layer="external")
        assert implicit == pytest.approx(explicit)

    def test_default_layer_is_external(self):
        """Omitting layer uses the external (k=0.048) default."""
        implicit = width_for_current(15, copper_weight_oz=2)
        explicit = width_for_current(15, copper_weight_oz=2, layer="external")
        assert implicit == pytest.approx(explicit)

    def test_higher_current_needs_wider_trace(self):
        narrow = width_for_current(5, copper_weight_oz=1)
        wide = width_for_current(20, copper_weight_oz=1)
        assert wide > narrow

    def test_thicker_copper_needs_narrower_trace(self):
        thin = width_for_current(15, copper_weight_oz=1)
        thick = width_for_current(15, copper_weight_oz=2)
        assert thick < thin

    def test_larger_delta_t_needs_narrower_trace(self):
        cold = width_for_current(15, copper_weight_oz=2, delta_t_c=10)
        hot = width_for_current(15, copper_weight_oz=2, delta_t_c=20)
        assert hot < cold

    def test_returns_positive_float(self):
        w = width_for_current(1, copper_weight_oz=1)
        assert isinstance(w, float)
        assert w > 0

    def test_exported_from_physics_package(self):
        """The function is re-exported from kicad_tools.physics."""
        assert width_for_current_exported is width_for_current


class TestWidthForCurrentValidation:
    """Invalid inputs raise ValueError."""

    def test_zero_current_raises(self):
        with pytest.raises(ValueError, match="current_a"):
            width_for_current(0, copper_weight_oz=2)

    def test_negative_current_raises(self):
        with pytest.raises(ValueError, match="current_a"):
            width_for_current(-5, copper_weight_oz=2)

    def test_zero_copper_weight_raises(self):
        with pytest.raises(ValueError, match="copper_weight_oz"):
            width_for_current(15, copper_weight_oz=0)

    def test_negative_copper_weight_raises(self):
        with pytest.raises(ValueError, match="copper_weight_oz"):
            width_for_current(15, copper_weight_oz=-1)

    def test_zero_delta_t_raises(self):
        with pytest.raises(ValueError, match="delta_t_c"):
            width_for_current(15, copper_weight_oz=2, delta_t_c=0)

    def test_negative_delta_t_raises(self):
        with pytest.raises(ValueError, match="delta_t_c"):
            width_for_current(15, copper_weight_oz=2, delta_t_c=-10)

    def test_invalid_layer_raises(self):
        with pytest.raises(ValueError, match="layer"):
            width_for_current(15, copper_weight_oz=2, layer="middle")

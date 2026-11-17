"""Tests for IPC-2221 ampacity width derivation (physics/ampacity.py, #4216)."""

from __future__ import annotations

import pytest

from kicad_tools.physics import (
    adiabatic_fusing_current as adiabatic_fusing_current_exported,
)
from kicad_tools.physics import (
    rms_current_for_duty_cycle as rms_current_for_duty_cycle_exported,
)
from kicad_tools.physics import width_for_current as width_for_current_exported
from kicad_tools.physics.ampacity import (
    adiabatic_fusing_current,
    rms_current_for_duty_cycle,
    width_for_current,
)


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


class TestRmsCurrentForDutyCycle:
    """Duty-cycled pulse train -> thermally equivalent RMS current (#4980)."""

    def test_golden_30a_peak_quarter_duty_over_2a_baseline(self):
        """sqrt(0.25*30^2 + 0.75*2^2) = sqrt(228) ~= 15.0997 A."""
        i_rms = rms_current_for_duty_cycle(30.0, duty_cycle=0.25, baseline_a=2.0)
        assert i_rms == pytest.approx(15.0997, abs=1e-3)

    def test_full_duty_is_the_peak(self):
        assert rms_current_for_duty_cycle(7.5, duty_cycle=1.0) == pytest.approx(7.5)

    def test_zero_baseline_scales_as_sqrt_duty(self):
        assert rms_current_for_duty_cycle(10.0, duty_cycle=0.25) == pytest.approx(5.0)

    def test_never_below_the_baseline(self):
        """A tiny-duty pulse on top of a steady current still yields >= baseline."""
        i_rms = rms_current_for_duty_cycle(30.0, duty_cycle=0.001, baseline_a=2.0)
        assert i_rms >= 2.0

    def test_monotonic_in_duty(self):
        low = rms_current_for_duty_cycle(10.0, duty_cycle=0.1)
        high = rms_current_for_duty_cycle(10.0, duty_cycle=0.9)
        assert high > low

    def test_zero_duty_raises(self):
        with pytest.raises(ValueError, match="duty_cycle"):
            rms_current_for_duty_cycle(10.0, duty_cycle=0.0)

    def test_duty_above_one_raises(self):
        with pytest.raises(ValueError, match="duty_cycle"):
            rms_current_for_duty_cycle(10.0, duty_cycle=1.5)

    def test_non_positive_peak_raises(self):
        with pytest.raises(ValueError, match="peak_a"):
            rms_current_for_duty_cycle(0.0, duty_cycle=0.5)

    def test_negative_baseline_raises(self):
        with pytest.raises(ValueError, match="baseline_a"):
            rms_current_for_duty_cycle(10.0, duty_cycle=0.5, baseline_a=-1.0)


class TestAdiabaticFusingCurrent:
    """Onderdonk adiabatic fusing current for copper traces (#4980)."""

    def test_golden_1mm_1oz_one_second(self):
        """1 mm / 1 oz / 1 s / 25 C ambient -> ~10.10 A (Onderdonk)."""
        i_fuse = adiabatic_fusing_current(1.0, copper_weight_oz=1.0, duration_s=1.0)
        assert i_fuse == pytest.approx(10.10, abs=0.05)

    def test_linear_in_cross_section(self):
        """Doubling width doubles the fusing current (I is linear in area)."""
        narrow = adiabatic_fusing_current(1.0, copper_weight_oz=1.0, duration_s=1.0)
        wide = adiabatic_fusing_current(2.0, copper_weight_oz=1.0, duration_s=1.0)
        assert wide == pytest.approx(2.0 * narrow, rel=1e-9)

    def test_doubles_copper_weight_doubles_current(self):
        thin = adiabatic_fusing_current(1.0, copper_weight_oz=1.0, duration_s=1.0)
        thick = adiabatic_fusing_current(1.0, copper_weight_oz=2.0, duration_s=1.0)
        assert thick == pytest.approx(2.0 * thin, rel=1e-9)

    def test_quarter_duration_doubles_current(self):
        """I scales as 1/sqrt(t): a 4x shorter pulse survives 2x the current."""
        long_pulse = adiabatic_fusing_current(1.0, copper_weight_oz=1.0, duration_s=1.0)
        short_pulse = adiabatic_fusing_current(1.0, copper_weight_oz=1.0, duration_s=0.25)
        assert short_pulse == pytest.approx(2.0 * long_pulse, rel=1e-9)

    def test_hotter_ambient_lowers_fusing_current(self):
        cool = adiabatic_fusing_current(1.0, copper_weight_oz=1.0, duration_s=1.0, ambient_c=25.0)
        hot = adiabatic_fusing_current(1.0, copper_weight_oz=1.0, duration_s=1.0, ambient_c=85.0)
        assert hot < cool

    def test_far_above_the_continuous_ipc_rating(self):
        """Sanity: a short pulse may exceed the steady-state IPC-2221 rating.

        The 1 oz / 1 mm trace's 10 C-rise continuous rating is ~2.3 A; the
        same trace survives several times that for a 1 s pulse.
        """
        continuous_width = width_for_current(2.3, copper_weight_oz=1.0, layer="external")
        assert continuous_width == pytest.approx(1.0, abs=0.15)
        assert adiabatic_fusing_current(1.0, copper_weight_oz=1.0, duration_s=1.0) > 2.3

    def test_non_positive_width_raises(self):
        with pytest.raises(ValueError, match="width_mm"):
            adiabatic_fusing_current(0.0, copper_weight_oz=1.0, duration_s=1.0)

    def test_non_positive_duration_raises(self):
        with pytest.raises(ValueError, match="duration_s"):
            adiabatic_fusing_current(1.0, copper_weight_oz=1.0, duration_s=0.0)

    def test_non_positive_copper_weight_raises(self):
        with pytest.raises(ValueError, match="copper_weight_oz"):
            adiabatic_fusing_current(1.0, copper_weight_oz=0.0, duration_s=1.0)

    def test_ambient_at_or_above_melting_raises(self):
        with pytest.raises(ValueError, match="ambient_c"):
            adiabatic_fusing_current(1.0, copper_weight_oz=1.0, duration_s=1.0, ambient_c=1200.0)

    def test_exported_from_physics_package(self):
        assert adiabatic_fusing_current_exported is adiabatic_fusing_current
        assert rms_current_for_duty_cycle_exported is rms_current_for_duty_cycle

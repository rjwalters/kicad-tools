"""Tests for kicad_tools.types.interval."""

from __future__ import annotations

import pytest

from kicad_tools.types.interval import Interval, UnitError

# ------------------------------------------------------------------
# Construction and validation
# ------------------------------------------------------------------


class TestConstruction:
    def test_basic_creation(self):
        iv = Interval(1.0, 2.0)
        assert iv.min == 1.0
        assert iv.max == 2.0
        assert iv.unit == ""

    def test_creation_with_unit(self):
        iv = Interval(10.0, 20.0, "ohm")
        assert iv.unit == "ohm"

    def test_min_greater_than_max_raises(self):
        with pytest.raises(ValueError, match="min.*must be <= max"):
            Interval(5.0, 3.0)

    def test_equal_bounds_ok(self):
        iv = Interval(7.0, 7.0)
        assert iv.is_exact

    def test_nan_min_raises(self):
        with pytest.raises(ValueError, match="NaN"):
            Interval(float("nan"), 1.0)

    def test_nan_max_raises(self):
        with pytest.raises(ValueError, match="NaN"):
            Interval(0.0, float("nan"))

    def test_frozen(self):
        iv = Interval(1.0, 2.0)
        with pytest.raises(AttributeError):
            iv.min = 0.0  # type: ignore[misc]


# ------------------------------------------------------------------
# Factory methods
# ------------------------------------------------------------------


class TestFactories:
    def test_from_center_rel(self):
        iv = Interval.from_center_rel(10_000, 0.05, "ohm")
        assert iv.min == pytest.approx(9_500.0)
        assert iv.max == pytest.approx(10_500.0)
        assert iv.unit == "ohm"

    def test_from_center_rel_zero_tolerance(self):
        iv = Interval.from_center_rel(100.0, 0.0)
        assert iv.is_exact
        assert iv.min == 100.0

    def test_from_center_abs(self):
        iv = Interval.from_center_abs(3.3, 0.1, "V")
        assert iv.min == pytest.approx(3.2)
        assert iv.max == pytest.approx(3.4)
        assert iv.unit == "V"

    def test_from_center_abs_negative_delta_treated_positive(self):
        iv = Interval.from_center_abs(5.0, -1.0)
        assert iv.min == 4.0
        assert iv.max == 6.0

    def test_exact(self):
        iv = Interval.exact(42.0, "mm")
        assert iv.min == 42.0
        assert iv.max == 42.0
        assert iv.unit == "mm"
        assert iv.is_exact


# ------------------------------------------------------------------
# Properties
# ------------------------------------------------------------------


class TestProperties:
    def test_center(self):
        iv = Interval(2.0, 8.0)
        assert iv.center == pytest.approx(5.0)

    def test_width(self):
        iv = Interval(2.0, 8.0)
        assert iv.width == pytest.approx(6.0)

    def test_exact_width_zero(self):
        iv = Interval.exact(5.0)
        assert iv.width == 0.0

    def test_is_exact_false_for_range(self):
        iv = Interval(1.0, 2.0)
        assert not iv.is_exact


# ------------------------------------------------------------------
# Set operations
# ------------------------------------------------------------------


class TestSetOperations:
    def test_contains_value_inside(self):
        iv = Interval(1.0, 10.0)
        assert iv.contains(5.0)

    def test_contains_value_at_boundary(self):
        iv = Interval(1.0, 10.0)
        assert iv.contains(1.0)
        assert iv.contains(10.0)

    def test_contains_value_outside(self):
        iv = Interval(1.0, 10.0)
        assert not iv.contains(0.99)
        assert not iv.contains(10.01)

    def test_contains_interval(self):
        outer = Interval(0.0, 10.0, "V")
        inner = Interval(2.0, 8.0, "V")
        assert outer.contains_interval(inner)
        assert not inner.contains_interval(outer)

    def test_contains_interval_unit_mismatch(self):
        a = Interval(0.0, 10.0, "V")
        b = Interval(2.0, 8.0, "A")
        with pytest.raises(UnitError):
            a.contains_interval(b)

    def test_overlaps_true(self):
        a = Interval(1.0, 5.0, "ohm")
        b = Interval(3.0, 8.0, "ohm")
        assert a.overlaps(b)
        assert b.overlaps(a)

    def test_overlaps_at_boundary(self):
        a = Interval(1.0, 5.0, "ohm")
        b = Interval(5.0, 10.0, "ohm")
        assert a.overlaps(b)

    def test_overlaps_false(self):
        a = Interval(1.0, 3.0, "ohm")
        b = Interval(4.0, 6.0, "ohm")
        assert not a.overlaps(b)

    def test_overlaps_unit_mismatch(self):
        a = Interval(1.0, 5.0, "V")
        b = Interval(3.0, 8.0, "A")
        with pytest.raises(UnitError):
            a.overlaps(b)

    def test_intersection(self):
        a = Interval(1.0, 5.0, "mm")
        b = Interval(3.0, 8.0, "mm")
        result = a.intersection(b)
        assert result == Interval(3.0, 5.0, "mm")

    def test_intersection_disjoint(self):
        a = Interval(1.0, 3.0)
        b = Interval(5.0, 7.0)
        assert a.intersection(b) is None

    def test_union(self):
        a = Interval(1.0, 5.0, "Hz")
        b = Interval(3.0, 8.0, "Hz")
        result = a.union(b)
        assert result == Interval(1.0, 8.0, "Hz")

    def test_union_disjoint(self):
        a = Interval(1.0, 3.0)
        b = Interval(5.0, 7.0)
        result = a.union(b)
        assert result == Interval(1.0, 7.0)


# ------------------------------------------------------------------
# Arithmetic: addition and subtraction
# ------------------------------------------------------------------


class TestAddSub:
    def test_add_intervals(self):
        a = Interval(1.0, 3.0, "V")
        b = Interval(2.0, 4.0, "V")
        result = a + b
        assert result == Interval(3.0, 7.0, "V")

    def test_add_scalar(self):
        iv = Interval(1.0, 3.0, "V")
        result = iv + 10.0
        assert result == Interval(11.0, 13.0, "V")

    def test_radd_scalar(self):
        iv = Interval(1.0, 3.0, "V")
        result = 10.0 + iv
        assert result == Interval(11.0, 13.0, "V")

    def test_add_unit_mismatch(self):
        a = Interval(1.0, 3.0, "V")
        b = Interval(2.0, 4.0, "A")
        with pytest.raises(UnitError):
            a + b

    def test_sub_intervals(self):
        a = Interval(5.0, 10.0, "ohm")
        b = Interval(1.0, 3.0, "ohm")
        result = a - b
        assert result == Interval(2.0, 9.0, "ohm")

    def test_sub_scalar(self):
        iv = Interval(5.0, 10.0)
        result = iv - 2.0
        assert result == Interval(3.0, 8.0)

    def test_rsub_scalar(self):
        iv = Interval(1.0, 3.0)
        result = 10.0 - iv
        assert result == Interval(7.0, 9.0)

    def test_sub_unit_mismatch(self):
        a = Interval(5.0, 10.0, "V")
        b = Interval(1.0, 3.0, "A")
        with pytest.raises(UnitError):
            a - b


# ------------------------------------------------------------------
# Arithmetic: multiplication and division
# ------------------------------------------------------------------


class TestMulDiv:
    def test_mul_intervals_same_unit(self):
        a = Interval(2.0, 3.0, "V")
        b = Interval(1.0, 4.0, "A")
        result = a * b
        assert result.min == pytest.approx(2.0)
        assert result.max == pytest.approx(12.0)
        assert result.unit == "V*A"

    def test_mul_scalar(self):
        iv = Interval(2.0, 5.0, "ohm")
        result = iv * 3.0
        assert result == Interval(6.0, 15.0, "ohm")

    def test_mul_negative_scalar(self):
        iv = Interval(2.0, 5.0, "ohm")
        result = iv * -1.0
        assert result == Interval(-5.0, -2.0, "ohm")

    def test_rmul_scalar(self):
        iv = Interval(2.0, 5.0, "ohm")
        result = 3.0 * iv
        assert result == Interval(6.0, 15.0, "ohm")

    def test_mul_dimensionless(self):
        a = Interval(2.0, 3.0)
        b = Interval(4.0, 5.0, "V")
        result = a * b
        assert result.unit == "V"

    def test_div_by_scalar(self):
        iv = Interval(6.0, 12.0, "V")
        result = iv / 3.0
        assert result == Interval(2.0, 4.0, "V")

    def test_div_by_interval(self):
        a = Interval(6.0, 12.0, "V")
        b = Interval(2.0, 3.0, "A")
        result = a / b
        assert result.min == pytest.approx(2.0)
        assert result.max == pytest.approx(6.0)
        assert result.unit == "V/A"

    def test_div_same_unit_dimensionless(self):
        a = Interval(6.0, 12.0, "ohm")
        b = Interval(2.0, 3.0, "ohm")
        result = a / b
        assert result.unit == ""

    def test_div_by_zero_scalar(self):
        iv = Interval(1.0, 2.0)
        with pytest.raises(ZeroDivisionError):
            iv / 0

    def test_div_by_interval_containing_zero(self):
        a = Interval(1.0, 2.0)
        b = Interval(-1.0, 1.0)
        with pytest.raises(ZeroDivisionError, match="containing zero"):
            a / b

    def test_mul_intervals_with_negative_range(self):
        a = Interval(-3.0, -1.0)
        b = Interval(2.0, 4.0)
        result = a * b
        assert result.min == pytest.approx(-12.0)
        assert result.max == pytest.approx(-2.0)


# ------------------------------------------------------------------
# Negation and absolute value
# ------------------------------------------------------------------


class TestNegAbs:
    def test_neg(self):
        iv = Interval(2.0, 5.0, "V")
        result = -iv
        assert result == Interval(-5.0, -2.0, "V")

    def test_abs_positive(self):
        iv = Interval(2.0, 5.0, "V")
        assert abs(iv) == iv

    def test_abs_negative(self):
        iv = Interval(-5.0, -2.0, "V")
        result = abs(iv)
        assert result == Interval(2.0, 5.0, "V")

    def test_abs_spanning_zero(self):
        iv = Interval(-3.0, 5.0)
        result = abs(iv)
        assert result == Interval(0.0, 5.0)


# ------------------------------------------------------------------
# Equality and hashing
# ------------------------------------------------------------------


class TestEqualityHash:
    def test_equal(self):
        a = Interval(1.0, 2.0, "V")
        b = Interval(1.0, 2.0, "V")
        assert a == b

    def test_not_equal_different_bounds(self):
        a = Interval(1.0, 2.0, "V")
        b = Interval(1.0, 3.0, "V")
        assert a != b

    def test_not_equal_different_unit(self):
        a = Interval(1.0, 2.0, "V")
        b = Interval(1.0, 2.0, "A")
        assert a != b

    def test_not_equal_non_interval(self):
        iv = Interval(1.0, 2.0)
        assert iv != "not an interval"

    def test_hashable(self):
        a = Interval(1.0, 2.0, "V")
        b = Interval(1.0, 2.0, "V")
        assert hash(a) == hash(b)
        assert len({a, b}) == 1


# ------------------------------------------------------------------
# Display
# ------------------------------------------------------------------


class TestDisplay:
    def test_repr_no_unit(self):
        iv = Interval(1.0, 2.0)
        assert repr(iv) == "Interval(1.0, 2.0)"

    def test_repr_with_unit(self):
        iv = Interval(1.0, 2.0, "ohm")
        assert repr(iv) == "Interval(1.0, 2.0, unit='ohm')"

    def test_str_range(self):
        iv = Interval(1.0, 2.0, "V")
        assert str(iv) == "[1.0, 2.0] V"

    def test_str_exact(self):
        iv = Interval.exact(3.3, "V")
        assert str(iv) == "3.3 V"

    def test_str_no_unit(self):
        iv = Interval(1.0, 2.0)
        assert str(iv) == "[1.0, 2.0]"

    def test_str_exact_no_unit(self):
        iv = Interval.exact(42.0)
        assert str(iv) == "42.0"


# ------------------------------------------------------------------
# Integration: constraint-style usage
# ------------------------------------------------------------------


class TestIntegration:
    """Test patterns that mirror real constraint scenarios."""

    def test_resistor_tolerance(self):
        r = Interval.from_center_rel(10_000, 0.05, "ohm")
        assert r.contains(10_000)
        assert r.contains(9_500)
        assert r.contains(10_500)
        assert not r.contains(9_499)

    def test_voltage_divider(self):
        """R2 / (R1 + R2) * Vin for a simple divider."""
        r1 = Interval.from_center_rel(10_000, 0.01, "ohm")
        r2 = Interval.from_center_rel(10_000, 0.01, "ohm")
        v_in = Interval.exact(5.0, "V")

        # R1 + R2  (same unit -> OK)
        r_total = r1 + r2

        # R2 / (R1+R2) gives a dimensionless ratio
        ratio = r2 / r_total
        assert ratio.unit == ""

        # ratio * V_in gives volts
        v_out = ratio * v_in
        assert v_out.unit == "V"
        assert v_out.contains(2.5)

    def test_unit_mismatch_caught(self):
        r = Interval(10_000, 10_000, "ohm")
        v = Interval(3.3, 3.3, "V")
        with pytest.raises(UnitError):
            r + v

"""Unit-aware interval type for parametric constraints.

Provides an ``Interval`` dataclass representing a bounded numeric range
with an optional unit string.  Arithmetic operations check unit
compatibility so that accidental ``ohm + V`` mistakes are caught at
runtime rather than silently producing wrong values.

Example::

    from kicad_tools.types import Interval

    r = Interval.from_center_rel(10e3, 0.05, "ohm")   # 10 kOhm +/- 5%
    v = Interval.exact(3.3, "V")                        # 3.3 V exactly

    # Set operations
    r.contains(10200.0)          # True
    r.overlaps(Interval(9000, 11000, "ohm"))  # True

    # Arithmetic (unit-checked)
    r2 = r + Interval(100, 200, "ohm")  # OK, same unit
    r * 2.0                               # scalar multiply keeps unit
    v * Interval(0.5, 1.5, "A")         # V * A -> "V*A"
"""

from __future__ import annotations

import math
from dataclasses import dataclass


class UnitError(TypeError):
    """Raised when an arithmetic operation mixes incompatible units."""


def _unit_product(a: str, b: str) -> str:
    """Combine two unit strings via multiplication.

    Empty units are treated as dimensionless.
    """
    if not a:
        return b
    if not b:
        return a
    return f"{a}*{b}"


def _unit_quotient(a: str, b: str) -> str:
    """Combine two unit strings via division.

    Empty units are treated as dimensionless.
    """
    if not b:
        return a
    if not a:
        return f"1/{b}"
    if a == b:
        return ""
    return f"{a}/{b}"


@dataclass(frozen=True)
class Interval:
    """A numeric interval ``[min, max]`` with an optional unit.

    Attributes:
        min: Lower bound (inclusive).
        max: Upper bound (inclusive).
        unit: Physical unit string (e.g. ``"ohm"``, ``"V"``).
              An empty string means dimensionless.
    """

    min: float
    max: float
    unit: str = ""

    def __post_init__(self) -> None:
        if math.isnan(self.min) or math.isnan(self.max):
            msg = "Interval bounds must not be NaN"
            raise ValueError(msg)
        if self.min > self.max:
            msg = f"min ({self.min}) must be <= max ({self.max})"
            raise ValueError(msg)

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def from_center_rel(cls, center: float, tolerance: float, unit: str = "") -> Interval:
        """Create from a center value and relative tolerance.

        Args:
            center: Nominal value.
            tolerance: Fractional tolerance (e.g. 0.05 for +/- 5 %).
            unit: Physical unit string.

        Returns:
            Interval spanning ``center * (1 - tolerance)`` to
            ``center * (1 + tolerance)``.

        Example::

            Interval.from_center_rel(10_000, 0.05, "ohm")
            # Interval(min=9500.0, max=10500.0, unit='ohm')
        """
        delta = abs(center * tolerance)
        return cls(center - delta, center + delta, unit)

    @classmethod
    def from_center_abs(cls, center: float, delta: float, unit: str = "") -> Interval:
        """Create from a center value and absolute delta.

        Args:
            center: Nominal value.
            delta: Absolute half-width (always treated as positive).
            unit: Physical unit string.

        Returns:
            Interval spanning ``center - |delta|`` to ``center + |delta|``.
        """
        delta = abs(delta)
        return cls(center - delta, center + delta, unit)

    @classmethod
    def exact(cls, value: float, unit: str = "") -> Interval:
        """Create a single-point (degenerate) interval.

        Args:
            value: The exact value.
            unit: Physical unit string.
        """
        return cls(value, value, unit)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def center(self) -> float:
        """Midpoint of the interval."""
        return (self.min + self.max) / 2.0

    @property
    def width(self) -> float:
        """Width of the interval (``max - min``)."""
        return self.max - self.min

    @property
    def is_exact(self) -> bool:
        """True if this is a single-point interval."""
        return self.min == self.max

    # ------------------------------------------------------------------
    # Set operations
    # ------------------------------------------------------------------

    def contains(self, value: float) -> bool:
        """Return True if *value* lies within ``[min, max]``."""
        return self.min <= value <= self.max

    def contains_interval(self, other: Interval) -> bool:
        """Return True if *other* is entirely within this interval."""
        self._check_same_unit(other, "contains_interval")
        return self.min <= other.min and other.max <= self.max

    def overlaps(self, other: Interval) -> bool:
        """Return True if this interval and *other* share any points."""
        self._check_same_unit(other, "overlaps")
        return self.min <= other.max and other.min <= self.max

    def intersection(self, other: Interval) -> Interval | None:
        """Return the overlap of two intervals, or ``None`` if disjoint."""
        self._check_same_unit(other, "intersection")
        lo = max(self.min, other.min)
        hi = min(self.max, other.max)
        if lo > hi:
            return None
        return Interval(lo, hi, self.unit)

    def union(self, other: Interval) -> Interval:
        """Return the smallest interval containing both.

        Note: this is the *hull*, not the set-theoretic union (which may
        not be an interval when the inputs are disjoint).
        """
        self._check_same_unit(other, "union")
        return Interval(min(self.min, other.min), max(self.max, other.max), self.unit)

    # ------------------------------------------------------------------
    # Arithmetic  (unit-checked)
    # ------------------------------------------------------------------

    def __add__(self, other: object) -> Interval:
        if isinstance(other, Interval):
            self._check_same_unit(other, "+")
            return Interval(self.min + other.min, self.max + other.max, self.unit)
        if isinstance(other, (int, float)):
            return Interval(self.min + other, self.max + other, self.unit)
        return NotImplemented

    def __radd__(self, other: object) -> Interval:
        if isinstance(other, (int, float)):
            return Interval(other + self.min, other + self.max, self.unit)
        return NotImplemented

    def __sub__(self, other: object) -> Interval:
        if isinstance(other, Interval):
            self._check_same_unit(other, "-")
            return Interval(self.min - other.max, self.max - other.min, self.unit)
        if isinstance(other, (int, float)):
            return Interval(self.min - other, self.max - other, self.unit)
        return NotImplemented

    def __rsub__(self, other: object) -> Interval:
        if isinstance(other, (int, float)):
            return Interval(other - self.max, other - self.min, self.unit)
        return NotImplemented

    def __mul__(self, other: object) -> Interval:
        if isinstance(other, Interval):
            products = (
                self.min * other.min,
                self.min * other.max,
                self.max * other.min,
                self.max * other.max,
            )
            return Interval(min(products), max(products), _unit_product(self.unit, other.unit))
        if isinstance(other, (int, float)):
            a, b = self.min * other, self.max * other
            return Interval(min(a, b), max(a, b), self.unit)
        return NotImplemented

    def __rmul__(self, other: object) -> Interval:
        if isinstance(other, (int, float)):
            a, b = other * self.min, other * self.max
            return Interval(min(a, b), max(a, b), self.unit)
        return NotImplemented

    def __truediv__(self, other: object) -> Interval:
        if isinstance(other, Interval):
            if other.min <= 0 <= other.max:
                msg = "Cannot divide by an interval containing zero"
                raise ZeroDivisionError(msg)
            quotients = (
                self.min / other.min,
                self.min / other.max,
                self.max / other.min,
                self.max / other.max,
            )
            return Interval(min(quotients), max(quotients), _unit_quotient(self.unit, other.unit))
        if isinstance(other, (int, float)):
            if other == 0:
                msg = "Cannot divide by zero"
                raise ZeroDivisionError(msg)
            a, b = self.min / other, self.max / other
            return Interval(min(a, b), max(a, b), self.unit)
        return NotImplemented

    def __neg__(self) -> Interval:
        return Interval(-self.max, -self.min, self.unit)

    def __abs__(self) -> Interval:
        if self.min >= 0:
            return self
        if self.max <= 0:
            return Interval(-self.max, -self.min, self.unit)
        return Interval(0.0, max(-self.min, self.max), self.unit)

    # ------------------------------------------------------------------
    # Comparison helpers
    # ------------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Interval):
            return NotImplemented
        return self.min == other.min and self.max == other.max and self.unit == other.unit

    def __hash__(self) -> int:
        return hash((self.min, self.max, self.unit))

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        if self.unit:
            return f"Interval({self.min}, {self.max}, unit={self.unit!r})"
        return f"Interval({self.min}, {self.max})"

    def __str__(self) -> str:
        if self.is_exact:
            return f"{self.min} {self.unit}".strip()
        suffix = f" {self.unit}" if self.unit else ""
        return f"[{self.min}, {self.max}]{suffix}"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_same_unit(self, other: Interval, op: str) -> None:
        if self.unit != other.unit:
            msg = (
                f"Cannot apply '{op}' to intervals with different units: "
                f"{self.unit!r} vs {other.unit!r}"
            )
            raise UnitError(msg)

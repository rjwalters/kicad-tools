"""Canonical type definitions for kicad-tools.

This module provides the authoritative definitions for commonly used enums
that were previously duplicated across the codebase. All modules should import
these types from here rather than defining their own.

Consolidated types:
- Severity: Validation severity levels (ERROR, WARNING, INFO)
- ERCSeverity: ERC-specific severity with EXCLUSION support
- RiskLevel: Analysis risk levels (LOW, MEDIUM, HIGH, CRITICAL)
- Layer: PCB layer enumeration with string values (KiCad compatible)
- CopperLayer: Copper layer indices for routing (integer values)
- LayoutStyle: Pin layout styles for symbol generation
"""

from __future__ import annotations

from enum import Enum

from .severity import SeverityMixin


class Severity(SeverityMixin, str, Enum):
    """Validation severity levels.

    This is the canonical Severity enum for DRC, validation, and general
    error reporting. Uses string values for JSON serialization compatibility.

    For ERC-specific use cases that require EXCLUSION, use ERCSeverity instead.
    """

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    def __str__(self) -> str:
        return self.value


class ERCSeverity(SeverityMixin, str, Enum):
    """ERC-specific severity levels with exclusion support.

    ERC violations can be excluded (marked as intentional), which requires
    a separate severity level not present in the standard Severity enum.
    """

    ERROR = "error"
    WARNING = "warning"
    EXCLUSION = "exclusion"

    def __str__(self) -> str:
        return self.value


class RiskLevel(str, Enum):
    """Risk level classification for analysis results.

    Used by congestion analysis, signal integrity analysis, and other
    risk assessment tools. Includes CRITICAL level for severe issues.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def from_string(cls, s: str, default: RiskLevel | None = None) -> RiskLevel:
        """Parse risk level from string.

        Args:
            s: String to parse (e.g., "high", "CRITICAL")
            default: Default value if no match found. If None, returns LOW.

        Returns:
            Matching RiskLevel enum member.
        """
        s_lower = s.lower().strip()
        for level in cls:
            if level.value == s_lower:
                return level
        return default if default is not None else cls.LOW


class Layer(str, Enum):
    """PCB layer enumeration with KiCad-compatible string values.

    This is the canonical Layer enum for use throughout the codebase when
    working with layer names. Values match KiCad's layer naming convention.

    For routing algorithms that need integer layer indices, use CopperLayer.
    """

    # Copper layers
    F_CU = "F.Cu"
    B_CU = "B.Cu"
    IN1_CU = "In1.Cu"
    IN2_CU = "In2.Cu"
    IN3_CU = "In3.Cu"
    IN4_CU = "In4.Cu"

    # Solder mask
    F_MASK = "F.Mask"
    B_MASK = "B.Mask"

    # Solder paste
    F_PASTE = "F.Paste"
    B_PASTE = "B.Paste"

    # Silkscreen
    F_SILKS = "F.SilkS"
    B_SILKS = "B.SilkS"

    # Courtyard
    F_CRTYD = "F.CrtYd"
    B_CRTYD = "B.CrtYd"

    # Fabrication
    F_FAB = "F.Fab"
    B_FAB = "B.Fab"

    # Board outline
    EDGE_CUTS = "Edge.Cuts"

    def __str__(self) -> str:
        return self.value

    @property
    def is_copper(self) -> bool:
        """Check if this is a copper layer."""
        return self.value.endswith(".Cu")

    @property
    def is_outer(self) -> bool:
        """Check if this is an outer (component) layer."""
        return self in (Layer.F_CU, Layer.B_CU)

    @property
    def is_front(self) -> bool:
        """Check if this is a front-side layer."""
        return self.value.startswith("F.")

    @property
    def is_back(self) -> bool:
        """Check if this is a back-side layer."""
        return self.value.startswith("B.")

    @classmethod
    def from_string(cls, name: str) -> Layer:
        """Convert a KiCad layer name to a Layer enum.

        Args:
            name: KiCad layer name like "F.Cu", "B.Cu", etc.

        Returns:
            The corresponding Layer enum member.

        Raises:
            ValueError: If the name doesn't match any known layer.
        """
        for layer in cls:
            if layer.value == name:
                return layer
        raise ValueError(f"Unknown KiCad layer name: {name}")

    @classmethod
    def copper_layers(cls) -> list[Layer]:
        """Get all copper layers in stack order (top to bottom)."""
        return [
            cls.F_CU,
            cls.IN1_CU,
            cls.IN2_CU,
            cls.IN3_CU,
            cls.IN4_CU,
            cls.B_CU,
        ]


class CopperLayer(Enum):
    """Copper layer indices for routing algorithms.

    This enum provides integer values for copper layers, useful for routing
    algorithms that operate on layer indices.  ``F_CU``..``B_CU`` keep their
    historical values (0..5, the positions in a 6-layer stack) so every
    existing 2/4/6-layer artifact is unchanged.  ``IN5_CU``..``IN30_CU``
    (Issue #6099) extend the enum to KiCad's full 32-copper-layer limit; their
    values (6..31) come *after* ``B_CU`` and are therefore NOT in physical
    stack order.

    Code that needs physical top-to-bottom order (via spans, "is this layer
    between these two") must compare :attr:`stack_order`, never ``value``.
    For the six historical members the two orders agree, so switching a
    comparison to ``stack_order`` is a no-op on 2/4/6-layer boards.

    For general layer references using KiCad names, use Layer instead.
    """

    F_CU = 0  # Top copper (outer)
    IN1_CU = 1  # Inner 1
    IN2_CU = 2  # Inner 2
    IN3_CU = 3  # Inner 3
    IN4_CU = 4  # Inner 4
    B_CU = 5  # Bottom copper (outer)
    # Issue #6099: inner layers 5..30 for 8+-layer boards (values after B_CU).
    IN5_CU = 6  # Inner 5
    IN6_CU = 7  # Inner 6
    IN7_CU = 8  # Inner 7
    IN8_CU = 9  # Inner 8
    IN9_CU = 10  # Inner 9
    IN10_CU = 11  # Inner 10
    IN11_CU = 12  # Inner 11
    IN12_CU = 13  # Inner 12
    IN13_CU = 14  # Inner 13
    IN14_CU = 15  # Inner 14
    IN15_CU = 16  # Inner 15
    IN16_CU = 17  # Inner 16
    IN17_CU = 18  # Inner 17
    IN18_CU = 19  # Inner 18
    IN19_CU = 20  # Inner 19
    IN20_CU = 21  # Inner 20
    IN21_CU = 22  # Inner 21
    IN22_CU = 23  # Inner 22
    IN23_CU = 24  # Inner 23
    IN24_CU = 25  # Inner 24
    IN25_CU = 26  # Inner 25
    IN26_CU = 27  # Inner 26
    IN27_CU = 28  # Inner 27
    IN28_CU = 29  # Inner 28
    IN29_CU = 30  # Inner 29
    IN30_CU = 31  # Inner 30

    @property
    def kicad_name(self) -> str:
        """Get the KiCad layer name for this copper layer."""
        return _COPPER_KICAD_NAMES[self]

    @property
    def inner_number(self) -> int:
        """``N`` of ``InN.Cu`` for an inner layer; 0 for ``F.Cu``/``B.Cu``."""
        if self is CopperLayer.F_CU or self is CopperLayer.B_CU:
            return 0
        return int(self.name[2:-3])  # "IN12_CU" -> 12

    @property
    def stack_order(self) -> int:
        """Physical top-to-bottom order key (Issue #6099).

        ``F.Cu`` is 0, ``InN.Cu`` is ``N`` and ``B.Cu`` is 31 (after KiCad's
        last possible inner layer, ``In30.Cu``).  Only the relative order is
        meaningful: use it to sort layers or to test whether a layer lies in a
        via's span.  It agrees with ``value`` ordering on the six historical
        members, so 2/4/6-layer behaviour is unchanged.
        """
        return _COPPER_STACK_ORDER[self]

    @property
    def is_outer(self) -> bool:
        """Check if this is an outer (component) layer."""
        return self in (CopperLayer.F_CU, CopperLayer.B_CU)

    @classmethod
    def from_kicad_name(cls, name: str) -> CopperLayer:
        """Convert a KiCad layer name to a CopperLayer enum.

        Args:
            name: KiCad layer name like "F.Cu", "B.Cu", "In1.Cu", etc.

        Returns:
            The corresponding CopperLayer enum member.

        Raises:
            ValueError: If the name doesn't match any known copper layer.
        """
        layer = _COPPER_BY_KICAD_NAME.get(name)
        if layer is None:
            raise ValueError(f"Unknown KiCad copper layer name: {name}")
        return layer

    def to_layer(self) -> Layer:
        """Convert to the corresponding Layer enum value."""
        return Layer.from_string(self.kicad_name)


_COPPER_KICAD_NAMES: dict[CopperLayer, str] = {
    layer: (
        "F.Cu"
        if layer is CopperLayer.F_CU
        else "B.Cu"
        if layer is CopperLayer.B_CU
        else f"In{layer.inner_number}.Cu"
    )
    for layer in CopperLayer
}
_COPPER_STACK_ORDER: dict[CopperLayer, int] = {
    layer: 31 if layer is CopperLayer.B_CU else layer.inner_number for layer in CopperLayer
}
_COPPER_BY_KICAD_NAME: dict[str, CopperLayer] = {
    name: layer for layer, name in _COPPER_KICAD_NAMES.items()
}


def copper_span(*layers: CopperLayer) -> tuple[CopperLayer, CopperLayer]:
    """Topmost and bottommost of ``layers`` in physical stack order.

    Issue #6099: a via's ``layers`` pair (or the union of several pairs when
    merging vias) spans every copper layer between these two.  Uses
    :attr:`CopperLayer.stack_order`, so ``In5.Cu`` and deeper sort above
    ``B.Cu`` even though their enum values are larger.
    """
    return (
        min(layers, key=lambda layer: layer.stack_order),
        max(layers, key=lambda layer: layer.stack_order),
    )


def copper_span_contains(first: CopperLayer, last: CopperLayer, layer: CopperLayer) -> bool:
    """True when ``layer`` lies between ``first`` and ``last`` (inclusive, either order)."""
    lo, hi = sorted((first.stack_order, last.stack_order))
    return lo <= layer.stack_order <= hi


def copper_layers_in_span(first: CopperLayer, last: CopperLayer) -> set[CopperLayer]:
    """Every :class:`CopperLayer` between ``first`` and ``last`` (inclusive)."""
    return {layer for layer in CopperLayer if copper_span_contains(first, last, layer)}


class LayoutStyle(str, Enum):
    """Pin layout styles for symbol generation.

    Determines how pins are arranged when generating KiCad symbols from
    datasheet information.
    """

    FUNCTIONAL = "functional"  # Group by function (power, GPIO, comms)
    PHYSICAL = "physical"  # Match IC package physical layout
    SIMPLE = "simple"  # Power top/bottom, signals left/right

    def __str__(self) -> str:
        return self.value


# Type aliases for backwards compatibility and documentation
ViolationSeverity = Severity  # Alias for code that uses "ViolationSeverity"


__all__ = [
    "Severity",
    "ERCSeverity",
    "RiskLevel",
    "Layer",
    "CopperLayer",
    "copper_span",
    "copper_span_contains",
    "copper_layers_in_span",
    "LayoutStyle",
    "ViolationSeverity",
]

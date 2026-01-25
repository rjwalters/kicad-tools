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
from typing import Any

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
    def from_string(cls, s: str, default: "RiskLevel | None" = None) -> "RiskLevel":
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
    def from_string(cls, name: str) -> "Layer":
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
    def copper_layers(cls) -> list["Layer"]:
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
    algorithms that operate on layer indices. The integer values correspond
    to the layer's position in a 6-layer stack (0 = top, 5 = bottom).

    For general layer references using KiCad names, use Layer instead.
    """

    F_CU = 0  # Top copper (outer)
    IN1_CU = 1  # Inner 1
    IN2_CU = 2  # Inner 2
    IN3_CU = 3  # Inner 3
    IN4_CU = 4  # Inner 4
    B_CU = 5  # Bottom copper (outer)

    @property
    def kicad_name(self) -> str:
        """Get the KiCad layer name for this copper layer."""
        return {
            CopperLayer.F_CU: "F.Cu",
            CopperLayer.IN1_CU: "In1.Cu",
            CopperLayer.IN2_CU: "In2.Cu",
            CopperLayer.IN3_CU: "In3.Cu",
            CopperLayer.IN4_CU: "In4.Cu",
            CopperLayer.B_CU: "B.Cu",
        }[self]

    @property
    def is_outer(self) -> bool:
        """Check if this is an outer (component) layer."""
        return self in (CopperLayer.F_CU, CopperLayer.B_CU)

    @classmethod
    def from_kicad_name(cls, name: str) -> "CopperLayer":
        """Convert a KiCad layer name to a CopperLayer enum.

        Args:
            name: KiCad layer name like "F.Cu", "B.Cu", "In1.Cu", etc.

        Returns:
            The corresponding CopperLayer enum member.

        Raises:
            ValueError: If the name doesn't match any known copper layer.
        """
        for layer in cls:
            if layer.kicad_name == name:
                return layer
        raise ValueError(f"Unknown KiCad copper layer name: {name}")

    def to_layer(self) -> Layer:
        """Convert to the corresponding Layer enum value."""
        return Layer.from_string(self.kicad_name)


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
    "LayoutStyle",
    "ViolationSeverity",
]

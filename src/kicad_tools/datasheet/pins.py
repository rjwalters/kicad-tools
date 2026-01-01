"""
Pin extraction data models and utilities.

Provides dataclasses for representing extracted pin definitions from datasheets,
including pin type inference and multi-package support.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExtractedPin:
    """
    Represents a pin extracted from a datasheet pin table.

    Attributes:
        number: Pin number (e.g., "1", "A1", "P1")
        name: Pin name/signal name (e.g., "VBAT", "PC13", "GPIO0")
        type: KiCad pin type (e.g., "power_in", "bidirectional", "input")
        type_confidence: Confidence score for the inferred type (0-1)
        type_source: How the type was determined ("inferred", "datasheet", "llm", "manual")
        description: Description of the pin function
        alt_functions: List of alternate functions for the pin
        electrical_type: Raw electrical type from datasheet (e.g., "I", "O", "I/O", "P")
        source_page: Page number where the pin was found (1-indexed)
    """

    number: str
    name: str
    type: str = "passive"
    type_confidence: float = 0.5
    type_source: str = "inferred"
    description: str = ""
    alt_functions: list[str] = field(default_factory=list)
    electrical_type: str | None = None
    source_page: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "number": self.number,
            "name": self.name,
            "type": self.type,
            "type_confidence": self.type_confidence,
            "type_source": self.type_source,
            "description": self.description,
            "alt_functions": self.alt_functions,
            "electrical_type": self.electrical_type,
            "source_page": self.source_page,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExtractedPin:
        """Create from dictionary."""
        return cls(
            number=data.get("number", ""),
            name=data.get("name", ""),
            type=data.get("type", "passive"),
            type_confidence=data.get("type_confidence", 0.5),
            type_source=data.get("type_source", "inferred"),
            description=data.get("description", ""),
            alt_functions=data.get("alt_functions", []),
            electrical_type=data.get("electrical_type"),
            source_page=data.get("source_page", 0),
        )

    def __repr__(self) -> str:
        return (
            f"ExtractedPin(number={self.number!r}, name={self.name!r}, "
            f"type={self.type!r}, confidence={self.type_confidence:.2f})"
        )


@dataclass
class PinTable:
    """
    Collection of extracted pins from a datasheet.

    Attributes:
        pins: List of extracted pin definitions
        package: Package name if known (e.g., "LQFP48", "BGA100")
        source_pages: List of page numbers where pins were extracted
        extraction_method: How the pins were extracted ("table", "llm", "manual")
        confidence: Overall extraction confidence (0-1)
    """

    pins: list[ExtractedPin] = field(default_factory=list)
    package: str | None = None
    source_pages: list[int] = field(default_factory=list)
    extraction_method: str = "table"
    confidence: float = 1.0

    @property
    def pin_count(self) -> int:
        """Number of pins in the table."""
        return len(self.pins)

    def get_pin(self, number: str) -> ExtractedPin | None:
        """Get a pin by its number."""
        for pin in self.pins:
            if pin.number == number:
                return pin
        return None

    def get_pins_by_type(self, pin_type: str) -> list[ExtractedPin]:
        """Get all pins of a specific type."""
        return [p for p in self.pins if p.type == pin_type]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "pins": [p.to_dict() for p in self.pins],
            "package": self.package,
            "source_pages": self.source_pages,
            "extraction_method": self.extraction_method,
            "confidence": self.confidence,
            "pin_count": self.pin_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PinTable:
        """Create from dictionary."""
        pins = [ExtractedPin.from_dict(p) for p in data.get("pins", [])]
        return cls(
            pins=pins,
            package=data.get("package"),
            source_pages=data.get("source_pages", []),
            extraction_method=data.get("extraction_method", "table"),
            confidence=data.get("confidence", 1.0),
        )

    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), indent=2)

    def to_csv(self) -> str:
        """Convert to CSV format."""
        import csv
        import io

        output = io.StringIO()
        writer = csv.writer(output)

        # Header
        writer.writerow(
            [
                "Number",
                "Name",
                "Type",
                "Type Confidence",
                "Description",
                "Alt Functions",
                "Electrical Type",
                "Source Page",
            ]
        )

        # Data rows
        for pin in self.pins:
            writer.writerow(
                [
                    pin.number,
                    pin.name,
                    pin.type,
                    f"{pin.type_confidence:.2f}",
                    pin.description,
                    ";".join(pin.alt_functions),
                    pin.electrical_type or "",
                    pin.source_page,
                ]
            )

        return output.getvalue()

    def to_markdown(self) -> str:
        """Convert to markdown table format."""
        lines = []

        # Header
        lines.append("| Number | Name | Type | Description |")
        lines.append("| --- | --- | --- | --- |")

        # Data rows
        for pin in self.pins:
            desc = pin.description[:50] + "..." if len(pin.description) > 50 else pin.description
            lines.append(f"| {pin.number} | {pin.name} | {pin.type} | {desc} |")

        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self.pins)

    def __iter__(self):
        return iter(self.pins)

    def __repr__(self) -> str:
        pkg = f", package={self.package!r}" if self.package else ""
        return f"PinTable({self.pin_count} pins{pkg})"

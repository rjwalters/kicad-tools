"""
Data models for parts database.

Defines dataclasses for parts, availability, and search results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional


class PartCategory(Enum):
    """Component categories."""

    RESISTOR = "resistor"
    CAPACITOR = "capacitor"
    INDUCTOR = "inductor"
    DIODE = "diode"
    TRANSISTOR = "transistor"
    IC = "ic"
    CONNECTOR = "connector"
    CRYSTAL = "crystal"
    LED = "led"
    SWITCH = "switch"
    RELAY = "relay"
    FUSE = "fuse"
    OTHER = "other"


class PackageType(Enum):
    """Package mounting type."""

    SMD = "smd"
    THROUGH_HOLE = "through_hole"
    UNKNOWN = "unknown"


@dataclass
class PartPrice:
    """Price break for a part."""

    quantity: int
    unit_price: float  # USD
    currency: str = "USD"

    @property
    def total_price(self) -> float:
        """Total price for this quantity."""
        return self.quantity * self.unit_price


@dataclass
class Part:
    """
    A component part from a supplier.

    Represents a part available from LCSC/JLCPCB or similar.
    """

    # Identification
    lcsc_part: str  # e.g., "C123456"
    mfr_part: str = ""  # Manufacturer part number
    manufacturer: str = ""  # Manufacturer name
    description: str = ""

    # Classification
    category: PartCategory = PartCategory.OTHER
    package: str = ""  # e.g., "0402", "SOIC-8"
    package_type: PackageType = PackageType.UNKNOWN

    # Specifications (common fields)
    value: str = ""  # e.g., "10k", "100nF"
    tolerance: str = ""  # e.g., "1%", "5%"
    voltage_rating: str = ""  # e.g., "50V"
    power_rating: str = ""  # e.g., "0.1W"
    temperature_range: str = ""  # e.g., "-40°C to +85°C"

    # Additional specs stored as dict
    specs: Dict[str, str] = field(default_factory=dict)

    # Stock and pricing
    stock: int = 0
    min_order: int = 1
    prices: List[PartPrice] = field(default_factory=list)

    # JLCPCB assembly info
    is_basic: bool = False  # JLCPCB basic part (no extra fee)
    is_preferred: bool = False  # JLCPCB preferred part

    # URLs and references
    datasheet_url: str = ""
    product_url: str = ""

    # Cache metadata
    fetched_at: Optional[datetime] = None

    @property
    def in_stock(self) -> bool:
        """Check if part is in stock."""
        return self.stock > 0

    @property
    def is_smd(self) -> bool:
        """Check if part is SMD."""
        return self.package_type == PackageType.SMD

    @property
    def best_price(self) -> Optional[float]:
        """Get lowest unit price available."""
        if not self.prices:
            return None
        return min(p.unit_price for p in self.prices)

    def price_at_quantity(self, qty: int) -> Optional[float]:
        """Get unit price for a specific quantity."""
        if not self.prices:
            return None
        # Find the price break for this quantity
        applicable = [p for p in self.prices if p.quantity <= qty]
        if not applicable:
            return self.prices[0].unit_price if self.prices else None
        return max(applicable, key=lambda p: p.quantity).unit_price

    def __str__(self) -> str:
        return f"{self.lcsc_part}: {self.description}"


@dataclass
class PartAvailability:
    """
    Availability check result for a BOM item.

    Maps a BOM entry to its matched part and availability status.
    """

    # BOM item info
    reference: str  # Reference designator (e.g., "U1")
    value: str  # Value from schematic
    footprint: str  # Footprint name
    lcsc_part: str  # LCSC part number from schematic/BOM

    # Match result
    part: Optional[Part] = None  # Matched part if found
    matched: bool = False
    in_stock: bool = False
    error: str = ""  # Error message if lookup failed

    # Quantity info
    quantity_needed: int = 1
    quantity_available: int = 0

    @property
    def sufficient_stock(self) -> bool:
        """Check if enough stock for needed quantity."""
        return self.quantity_available >= self.quantity_needed

    @property
    def status(self) -> str:
        """Human-readable status."""
        if self.error:
            return f"Error: {self.error}"
        if not self.matched:
            return "Not found"
        if not self.in_stock:
            return "Out of stock"
        if not self.sufficient_stock:
            return f"Low stock ({self.quantity_available} available)"
        return "OK"


@dataclass
class SearchResult:
    """Result from a parts search."""

    query: str
    parts: List[Part] = field(default_factory=list)
    total_count: int = 0
    page: int = 1
    page_size: int = 20

    @property
    def has_more(self) -> bool:
        """Check if more results available."""
        return self.total_count > self.page * self.page_size

    def __len__(self) -> int:
        return len(self.parts)

    def __iter__(self):
        return iter(self.parts)


@dataclass
class BOMAvailability:
    """
    Availability check results for an entire BOM.

    Aggregates availability checks for all BOM items.
    """

    items: List[PartAvailability] = field(default_factory=list)
    checked_at: Optional[datetime] = None

    @property
    def all_available(self) -> bool:
        """Check if all items are available."""
        return all(item.sufficient_stock for item in self.items)

    @property
    def missing_parts(self) -> List[PartAvailability]:
        """Get items that couldn't be found."""
        return [item for item in self.items if not item.matched]

    @property
    def out_of_stock(self) -> List[PartAvailability]:
        """Get items that are out of stock."""
        return [item for item in self.items if item.matched and not item.in_stock]

    @property
    def low_stock(self) -> List[PartAvailability]:
        """Get items with insufficient stock."""
        return [
            item
            for item in self.items
            if item.matched and item.in_stock and not item.sufficient_stock
        ]

    @property
    def available(self) -> List[PartAvailability]:
        """Get items with sufficient stock."""
        return [item for item in self.items if item.sufficient_stock]

    def summary(self) -> Dict[str, int]:
        """Get summary counts."""
        return {
            "total": len(self.items),
            "available": len(self.available),
            "missing": len(self.missing_parts),
            "out_of_stock": len(self.out_of_stock),
            "low_stock": len(self.low_stock),
        }

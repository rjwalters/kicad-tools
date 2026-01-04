"""
Part availability checking for BOM components.

Checks LCSC stock levels and identifies availability issues before ordering.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.parts.models import Part
    from kicad_tools.schema.bom import BOM, BOMItem

logger = logging.getLogger(__name__)


class AvailabilityStatus(Enum):
    """Availability status for a part."""

    AVAILABLE = "available"
    LOW_STOCK = "low_stock"
    OUT_OF_STOCK = "out_of_stock"
    DISCONTINUED = "discontinued"
    UNKNOWN = "unknown"
    NO_LCSC = "no_lcsc"
    NOT_FOUND = "not_found"


@dataclass
class AlternativePart:
    """An alternative part suggestion."""

    lcsc_part: str
    mfr_part: str
    description: str
    stock: int
    price_diff: float | None  # Price difference vs original (None if unknown)
    is_basic: bool


@dataclass
class PartAvailabilityResult:
    """Availability check result for a single BOM item."""

    # BOM item info
    reference: str
    value: str
    footprint: str
    mpn: str | None
    lcsc_part: str | None

    # Quantity info
    quantity_needed: int
    quantity_available: int

    # Status
    status: AvailabilityStatus
    in_stock: bool

    # Ordering info
    min_order_qty: int | None = None
    price_breaks: list[tuple[int, float]] = field(default_factory=list)

    # Lead time
    lead_time_days: int | None = None

    # Alternatives (only populated if not available)
    alternatives: list[AlternativePart] = field(default_factory=list)

    # Error info
    error: str | None = None

    @property
    def sufficient_stock(self) -> bool:
        """Check if enough stock for needed quantity."""
        return self.quantity_available >= self.quantity_needed

    @property
    def unit_price(self) -> float | None:
        """Get unit price for needed quantity."""
        if not self.price_breaks:
            return None
        # Find applicable price break
        applicable = [p for qty, p in self.price_breaks if qty <= self.quantity_needed]
        if applicable:
            return applicable[-1]  # Highest quantity break that applies
        return self.price_breaks[0][1] if self.price_breaks else None

    @property
    def extended_price(self) -> float | None:
        """Get total price for needed quantity."""
        unit = self.unit_price
        if unit is None:
            return None
        return unit * self.quantity_needed

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "reference": self.reference,
            "value": self.value,
            "footprint": self.footprint,
            "mpn": self.mpn,
            "lcsc_part": self.lcsc_part,
            "quantity_needed": self.quantity_needed,
            "quantity_available": self.quantity_available,
            "status": self.status.value,
            "in_stock": self.in_stock,
            "sufficient_stock": self.sufficient_stock,
            "min_order_qty": self.min_order_qty,
            "price_breaks": self.price_breaks,
            "unit_price": self.unit_price,
            "extended_price": self.extended_price,
            "lead_time_days": self.lead_time_days,
            "alternatives": [
                {
                    "lcsc_part": alt.lcsc_part,
                    "mfr_part": alt.mfr_part,
                    "description": alt.description,
                    "stock": alt.stock,
                    "price_diff": alt.price_diff,
                    "is_basic": alt.is_basic,
                }
                for alt in self.alternatives
            ],
            "error": self.error,
        }


@dataclass
class BOMAvailabilityResult:
    """Availability check results for an entire BOM."""

    items: list[PartAvailabilityResult] = field(default_factory=list)
    checked_at: datetime | None = None
    quantity_multiplier: int = 1  # Number of boards

    @property
    def available(self) -> list[PartAvailabilityResult]:
        """Get items with sufficient stock."""
        return [item for item in self.items if item.sufficient_stock]

    @property
    def low_stock(self) -> list[PartAvailabilityResult]:
        """Get items with insufficient stock (but some stock)."""
        return [item for item in self.items if item.in_stock and not item.sufficient_stock]

    @property
    def out_of_stock(self) -> list[PartAvailabilityResult]:
        """Get items that are out of stock."""
        return [item for item in self.items if item.status == AvailabilityStatus.OUT_OF_STOCK]

    @property
    def missing(self) -> list[PartAvailabilityResult]:
        """Get items that couldn't be found or have no LCSC number."""
        return [
            item
            for item in self.items
            if item.status in (AvailabilityStatus.NOT_FOUND, AvailabilityStatus.NO_LCSC)
        ]

    @property
    def all_available(self) -> bool:
        """Check if all items have sufficient stock."""
        return all(item.sufficient_stock for item in self.items)

    @property
    def total_cost(self) -> float | None:
        """Calculate total cost for all parts (None if any price is unknown)."""
        total = 0.0
        for item in self.items:
            if item.extended_price is None:
                return None
            total += item.extended_price
        return total

    def summary(self) -> dict:
        """Get summary statistics."""
        return {
            "total_items": len(self.items),
            "available": len(self.available),
            "low_stock": len(self.low_stock),
            "out_of_stock": len(self.out_of_stock),
            "missing": len(self.missing),
            "all_available": self.all_available,
            "total_cost": self.total_cost,
            "quantity_multiplier": self.quantity_multiplier,
        }

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "summary": self.summary(),
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "items": [item.to_dict() for item in self.items],
        }


class LCSCAvailabilityChecker:
    """Check part availability on LCSC/JLCPCB."""

    # Default minimum stock threshold (parts below this are "low stock")
    DEFAULT_LOW_STOCK_THRESHOLD = 100

    def __init__(
        self,
        low_stock_threshold: int = DEFAULT_LOW_STOCK_THRESHOLD,
        find_alternatives: bool = True,
        max_alternatives: int = 3,
    ):
        """
        Initialize the availability checker.

        Args:
            low_stock_threshold: Stock level below which parts are considered low stock
            find_alternatives: Whether to search for alternatives for unavailable parts
            max_alternatives: Maximum number of alternatives to suggest
        """
        self.low_stock_threshold = low_stock_threshold
        self.find_alternatives = find_alternatives
        self.max_alternatives = max_alternatives
        self._client = None

    def _get_client(self):
        """Get or create LCSC client."""
        if self._client is None:
            from kicad_tools.parts import LCSCClient

            self._client = LCSCClient()
        return self._client

    def check_bom(
        self,
        bom: BOM,
        quantity: int = 1,
    ) -> BOMAvailabilityResult:
        """
        Check availability for all items in a BOM.

        Args:
            bom: Bill of Materials to check
            quantity: Number of boards (multiplies component quantities)

        Returns:
            BOMAvailabilityResult with availability status for each item
        """
        client = self._get_client()
        results: list[PartAvailabilityResult] = []

        # Group BOM items and collect LCSC numbers
        groups = bom.grouped()
        lcsc_parts = []
        for group in groups:
            if group.lcsc:
                lcsc_parts.append(group.lcsc)

        # Bulk fetch parts
        parts_map = client.lookup_many(list(set(lcsc_parts))) if lcsc_parts else {}

        # Check each group
        for group in groups:
            # Skip DNP items
            if group.items and group.items[0].dnp:
                continue

            qty_needed = group.quantity * quantity
            result = self._check_item(
                reference=group.references.split(",")[0].strip(),
                value=group.value,
                footprint=group.footprint,
                mpn=group.mpn or None,
                lcsc=group.lcsc or None,
                quantity_needed=qty_needed,
                parts_map=parts_map,
            )
            results.append(result)

        return BOMAvailabilityResult(
            items=results,
            checked_at=datetime.now(),
            quantity_multiplier=quantity,
        )

    def check_items(
        self,
        items: list[BOMItem],
        quantity: int = 1,
    ) -> BOMAvailabilityResult:
        """
        Check availability for a list of BOM items.

        Args:
            items: List of BOM items to check
            quantity: Number of boards (multiplies component quantities)

        Returns:
            BOMAvailabilityResult with availability status for each item
        """
        client = self._get_client()
        results: list[PartAvailabilityResult] = []

        # Collect LCSC numbers
        lcsc_parts = [item.lcsc for item in items if item.lcsc]

        # Bulk fetch parts
        parts_map = client.lookup_many(list(set(lcsc_parts))) if lcsc_parts else {}

        # Check each item
        for item in items:
            if item.dnp or item.is_virtual:
                continue

            qty_needed = item.quantity if hasattr(item, "quantity") else 1
            qty_needed *= quantity

            result = self._check_item(
                reference=item.reference,
                value=item.value,
                footprint=item.footprint,
                mpn=item.mpn or None,
                lcsc=item.lcsc or None,
                quantity_needed=qty_needed,
                parts_map=parts_map,
            )
            results.append(result)

        return BOMAvailabilityResult(
            items=results,
            checked_at=datetime.now(),
            quantity_multiplier=quantity,
        )

    def _check_item(
        self,
        reference: str,
        value: str,
        footprint: str,
        mpn: str | None,
        lcsc: str | None,
        quantity_needed: int,
        parts_map: dict[str, Part],
    ) -> PartAvailabilityResult:
        """Check availability for a single item."""
        # No LCSC part number
        if not lcsc:
            return PartAvailabilityResult(
                reference=reference,
                value=value,
                footprint=footprint,
                mpn=mpn,
                lcsc_part=None,
                quantity_needed=quantity_needed,
                quantity_available=0,
                status=AvailabilityStatus.NO_LCSC,
                in_stock=False,
                error="No LCSC part number",
            )

        # Look up part
        part = parts_map.get(lcsc.upper())
        if part is None:
            return PartAvailabilityResult(
                reference=reference,
                value=value,
                footprint=footprint,
                mpn=mpn,
                lcsc_part=lcsc,
                quantity_needed=quantity_needed,
                quantity_available=0,
                status=AvailabilityStatus.NOT_FOUND,
                in_stock=False,
                error="Part not found in LCSC database",
            )

        # Determine status
        status = self._determine_status(part.stock, quantity_needed)
        in_stock = part.stock > 0

        # Extract price breaks
        price_breaks = [(p.quantity, p.unit_price) for p in part.prices]

        # Find alternatives if not available
        alternatives = []
        if self.find_alternatives and status != AvailabilityStatus.AVAILABLE and mpn:
            alternatives = self._find_alternatives(mpn, part, quantity_needed)

        return PartAvailabilityResult(
            reference=reference,
            value=value,
            footprint=footprint,
            mpn=mpn or part.mfr_part,
            lcsc_part=lcsc,
            quantity_needed=quantity_needed,
            quantity_available=part.stock,
            status=status,
            in_stock=in_stock,
            min_order_qty=part.min_order,
            price_breaks=price_breaks,
            lead_time_days=None,  # LCSC API doesn't provide this currently
            alternatives=alternatives,
        )

    def _determine_status(self, stock: int, needed: int) -> AvailabilityStatus:
        """Determine availability status based on stock levels."""
        if stock == 0:
            return AvailabilityStatus.OUT_OF_STOCK
        elif stock < needed:
            return AvailabilityStatus.LOW_STOCK
        elif stock < max(needed * 2, self.low_stock_threshold):
            return AvailabilityStatus.LOW_STOCK
        else:
            return AvailabilityStatus.AVAILABLE

    def _find_alternatives(
        self,
        mpn: str,
        original: Part,
        quantity_needed: int,
    ) -> list[AlternativePart]:
        """Find alternative parts for an unavailable part."""
        client = self._get_client()
        alternatives = []

        try:
            # Search using MPN
            results = client.search(mpn, in_stock=True, page_size=10)

            original_price = original.price_at_quantity(quantity_needed)

            for part in results.parts:
                # Skip the original part
                if part.lcsc_part.upper() == original.lcsc_part.upper():
                    continue

                # Skip parts with insufficient stock
                if part.stock < quantity_needed:
                    continue

                # Calculate price difference
                price_diff = None
                if original_price:
                    part_price = part.price_at_quantity(quantity_needed)
                    if part_price:
                        price_diff = part_price - original_price

                alternatives.append(
                    AlternativePart(
                        lcsc_part=part.lcsc_part,
                        mfr_part=part.mfr_part,
                        description=part.description,
                        stock=part.stock,
                        price_diff=price_diff,
                        is_basic=part.is_basic,
                    )
                )

                if len(alternatives) >= self.max_alternatives:
                    break

        except Exception as e:
            logger.warning(f"Failed to find alternatives for {mpn}: {e}")

        return alternatives

    def close(self) -> None:
        """Close the client connection."""
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

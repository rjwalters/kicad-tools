"""
Assembly validation for JLCPCB/LCSC BOM components.

Validates BOM availability and categorizes parts by JLCPCB tier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.schema.bom import BOM, BOMGroup


class PartTier(Enum):
    """JLCPCB part tier classification."""

    BASIC = "basic"  # No handling fee, fast lead time
    EXTENDED = "extended"  # $3 handling fee per unique part
    GLOBAL = "global"  # Global sourcing, longer lead time
    UNKNOWN = "unknown"


class ValidationStatus(Enum):
    """Validation status for a part."""

    AVAILABLE = "available"
    LOW_STOCK = "low_stock"
    OUT_OF_STOCK = "out_of_stock"
    NOT_FOUND = "not_found"
    NO_LCSC = "no_lcsc"
    INVALID_FORMAT = "invalid_format"


@dataclass
class PartValidationResult:
    """Validation result for a single BOM item."""

    # BOM info
    references: str
    value: str
    footprint: str
    quantity: int
    lcsc_part: str | None

    # Validation result
    status: ValidationStatus
    tier: PartTier

    # Stock info
    stock: int = 0
    in_stock: bool = False

    # Part details (if found)
    mfr_part: str = ""
    description: str = ""

    # Error info
    error: str | None = None

    @property
    def status_symbol(self) -> str:
        """Get status symbol for display."""
        if self.status == ValidationStatus.AVAILABLE:
            return "✓"
        elif self.status == ValidationStatus.LOW_STOCK:
            return "⚠"
        elif self.status == ValidationStatus.OUT_OF_STOCK:
            return "✗"
        elif self.status == ValidationStatus.NOT_FOUND:
            return "?"
        elif self.status == ValidationStatus.NO_LCSC:
            return "-"
        else:
            return "!"

    @property
    def status_text(self) -> str:
        """Get human-readable status text."""
        if self.status == ValidationStatus.AVAILABLE:
            return "Available"
        elif self.status == ValidationStatus.LOW_STOCK:
            return f"Low ({self.stock})"
        elif self.status == ValidationStatus.OUT_OF_STOCK:
            return "OOS"
        elif self.status == ValidationStatus.NOT_FOUND:
            return "Not Found"
        elif self.status == ValidationStatus.NO_LCSC:
            return "No LCSC"
        elif self.status == ValidationStatus.INVALID_FORMAT:
            return "Invalid"
        return "Unknown"

    @property
    def tier_text(self) -> str:
        """Get tier text for display."""
        return self.tier.value.capitalize() if self.tier != PartTier.UNKNOWN else "-"

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "references": self.references,
            "value": self.value,
            "footprint": self.footprint,
            "quantity": self.quantity,
            "lcsc_part": self.lcsc_part,
            "status": self.status.value,
            "tier": self.tier.value,
            "stock": self.stock,
            "in_stock": self.in_stock,
            "mfr_part": self.mfr_part,
            "description": self.description,
            "error": self.error,
        }


@dataclass
class AssemblyValidationResult:
    """Result of validating a BOM for JLCPCB assembly."""

    items: list[PartValidationResult] = field(default_factory=list)
    validated_at: datetime | None = None

    # Extended part handling fee (USD)
    EXTENDED_PART_FEE = 3.0

    @property
    def basic_parts(self) -> list[PartValidationResult]:
        """Get basic tier parts (no handling fee)."""
        return [
            item
            for item in self.items
            if item.tier == PartTier.BASIC and item.status == ValidationStatus.AVAILABLE
        ]

    @property
    def extended_parts(self) -> list[PartValidationResult]:
        """Get extended tier parts ($3 fee each)."""
        return [
            item
            for item in self.items
            if item.tier == PartTier.EXTENDED and item.status == ValidationStatus.AVAILABLE
        ]

    @property
    def out_of_stock(self) -> list[PartValidationResult]:
        """Get out of stock parts (need consignment/wait)."""
        return [item for item in self.items if item.status == ValidationStatus.OUT_OF_STOCK]

    @property
    def low_stock(self) -> list[PartValidationResult]:
        """Get low stock parts (may need attention)."""
        return [item for item in self.items if item.status == ValidationStatus.LOW_STOCK]

    @property
    def missing_lcsc(self) -> list[PartValidationResult]:
        """Get parts without LCSC numbers."""
        return [item for item in self.items if item.status == ValidationStatus.NO_LCSC]

    @property
    def not_found(self) -> list[PartValidationResult]:
        """Get parts with invalid LCSC numbers."""
        return [item for item in self.items if item.status == ValidationStatus.NOT_FOUND]

    @property
    def invalid_format(self) -> list[PartValidationResult]:
        """Get parts with invalid LCSC format."""
        return [item for item in self.items if item.status == ValidationStatus.INVALID_FORMAT]

    @property
    def available_count(self) -> int:
        """Count of available parts (basic + extended available)."""
        return len(
            [
                item
                for item in self.items
                if item.status in (ValidationStatus.AVAILABLE, ValidationStatus.LOW_STOCK)
            ]
        )

    @property
    def assembly_ready(self) -> bool:
        """True if all parts are available for assembly."""
        problem_statuses = {
            ValidationStatus.OUT_OF_STOCK,
            ValidationStatus.NOT_FOUND,
            ValidationStatus.NO_LCSC,
            ValidationStatus.INVALID_FORMAT,
        }
        return not any(item.status in problem_statuses for item in self.items)

    @property
    def extended_fee(self) -> float:
        """Total extended parts fee (unique parts * $3)."""
        return len(self.extended_parts) * self.EXTENDED_PART_FEE

    def summary(self) -> dict:
        """Get summary statistics."""
        return {
            "total_items": len(self.items),
            "available": self.available_count,
            "basic_parts": len(self.basic_parts),
            "extended_parts": len(self.extended_parts),
            "low_stock": len(self.low_stock),
            "out_of_stock": len(self.out_of_stock),
            "missing_lcsc": len(self.missing_lcsc),
            "not_found": len(self.not_found),
            "assembly_ready": self.assembly_ready,
            "extended_fee": self.extended_fee,
        }

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "summary": self.summary(),
            "validated_at": self.validated_at.isoformat() if self.validated_at else None,
            "items": [item.to_dict() for item in self.items],
        }

    def format_table(self) -> str:
        """Format results as a human-readable table."""
        if not self.items:
            return "No components to validate."

        lines = []

        # Header
        lines.append("")
        lines.append(
            f"{'LCSC Part #':<12} {'Component':<20} {'Tier':<10} {'Stock':<10} {'Status':<12}"
        )
        lines.append("-" * 66)

        # Items
        for item in self.items:
            lcsc = item.lcsc_part or "(none)"
            component = f"{item.value[:18]}" if len(item.value) > 18 else item.value
            tier = item.tier_text
            stock = str(item.stock) if item.stock > 0 else "-"
            status = f"{item.status_symbol} {item.status_text}"

            lines.append(f"{lcsc:<12} {component:<20} {tier:<10} {stock:<10} {status:<12}")

        # Summary
        lines.append("-" * 66)
        summary = self.summary()
        lines.append(f"\nSummary: {summary['available']}/{summary['total_items']} parts available")
        lines.append(f"  Basic: {summary['basic_parts']} parts (no handling fee)")
        lines.append(
            f"  Extended: {summary['extended_parts']} parts "
            f"(${self.EXTENDED_PART_FEE:.0f} fee each = ${summary['extended_fee']:.2f})"
        )

        if summary["out_of_stock"] > 0:
            lines.append(f"  Out of Stock: {summary['out_of_stock']} parts (requires consignment)")
        if summary["missing_lcsc"] > 0:
            lines.append(f"  Missing LCSC: {summary['missing_lcsc']} parts (needs part number)")
        if summary["not_found"] > 0:
            lines.append(f"  Not Found: {summary['not_found']} parts (invalid LCSC number)")

        if self.assembly_ready:
            lines.append("\n✓ All parts available for JLCPCB assembly")
        else:
            lines.append("\n✗ Some parts unavailable - review before ordering")

        return "\n".join(lines)


class AssemblyValidator:
    """Validates BOM for JLCPCB assembly."""

    # LCSC part number pattern: C followed by digits
    LCSC_PATTERN = r"^C\d+$"

    # Low stock threshold
    LOW_STOCK_THRESHOLD = 100

    def __init__(self, use_cache: bool = True, timeout: float = 30.0):
        """
        Initialize the validator.

        Args:
            use_cache: Whether to cache API responses
            timeout: API request timeout in seconds
        """
        self.use_cache = use_cache
        self.timeout = timeout
        self._client = None

    def _get_client(self):
        """Get or create LCSC client."""
        if self._client is None:
            from kicad_tools.parts import LCSCClient

            self._client = LCSCClient(use_cache=self.use_cache, timeout=self.timeout)
        return self._client

    def validate_bom(self, bom: BOM, quantity: int = 1) -> AssemblyValidationResult:
        """
        Validate a BOM for JLCPCB assembly.

        Args:
            bom: Bill of Materials to validate
            quantity: Number of boards (multiplies component quantities)

        Returns:
            AssemblyValidationResult with validation results
        """
        import re

        client = self._get_client()
        results: list[PartValidationResult] = []

        # Group BOM and collect LCSC numbers
        groups = bom.grouped()
        valid_lcsc_parts = []

        for group in groups:
            # Skip DNP items
            if group.items and group.items[0].dnp:
                continue

            if group.lcsc:
                lcsc_upper = group.lcsc.upper()
                # Normalize: add C prefix if missing
                if not lcsc_upper.startswith("C"):
                    lcsc_upper = f"C{lcsc_upper}"
                # Validate format
                if re.match(self.LCSC_PATTERN, lcsc_upper):
                    valid_lcsc_parts.append(lcsc_upper)

        # Bulk fetch parts
        parts_map = client.lookup_many(list(set(valid_lcsc_parts))) if valid_lcsc_parts else {}

        # Validate each group
        for group in groups:
            # Skip DNP items
            if group.items and group.items[0].dnp:
                continue

            result = self._validate_group(group, quantity, parts_map)
            results.append(result)

        return AssemblyValidationResult(
            items=results,
            validated_at=datetime.now(),
        )

    def _validate_group(
        self,
        group: BOMGroup,
        quantity: int,
        parts_map: dict,
    ) -> PartValidationResult:
        """Validate a single BOM group."""
        import re

        lcsc = group.lcsc
        qty_needed = group.quantity * quantity

        # No LCSC part number
        if not lcsc:
            return PartValidationResult(
                references=group.references,
                value=group.value,
                footprint=group.footprint,
                quantity=qty_needed,
                lcsc_part=None,
                status=ValidationStatus.NO_LCSC,
                tier=PartTier.UNKNOWN,
            )

        # Normalize LCSC part number
        lcsc_upper = lcsc.upper()
        if not lcsc_upper.startswith("C"):
            lcsc_upper = f"C{lcsc_upper}"

        # Check format
        if not re.match(self.LCSC_PATTERN, lcsc_upper):
            return PartValidationResult(
                references=group.references,
                value=group.value,
                footprint=group.footprint,
                quantity=qty_needed,
                lcsc_part=lcsc,
                status=ValidationStatus.INVALID_FORMAT,
                tier=PartTier.UNKNOWN,
                error=f"Invalid LCSC format: {lcsc}",
            )

        # Look up part
        part = parts_map.get(lcsc_upper)
        if part is None:
            return PartValidationResult(
                references=group.references,
                value=group.value,
                footprint=group.footprint,
                quantity=qty_needed,
                lcsc_part=lcsc_upper,
                status=ValidationStatus.NOT_FOUND,
                tier=PartTier.UNKNOWN,
                error="Part not found in LCSC database",
            )

        # Determine tier
        if part.is_basic:
            tier = PartTier.BASIC
        elif part.is_preferred:
            tier = PartTier.EXTENDED  # Preferred is still extended tier pricing
        else:
            tier = PartTier.EXTENDED

        # Determine status
        if part.stock == 0:
            status = ValidationStatus.OUT_OF_STOCK
        elif part.stock < max(qty_needed * 2, self.LOW_STOCK_THRESHOLD):
            status = ValidationStatus.LOW_STOCK
        else:
            status = ValidationStatus.AVAILABLE

        return PartValidationResult(
            references=group.references,
            value=group.value,
            footprint=group.footprint,
            quantity=qty_needed,
            lcsc_part=lcsc_upper,
            status=status,
            tier=tier,
            stock=part.stock,
            in_stock=part.stock > 0,
            mfr_part=part.mfr_part,
            description=part.description,
        )

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


def validate_assembly(
    schematic_path: str,
    quantity: int = 1,
    use_cache: bool = True,
) -> AssemblyValidationResult:
    """
    Validate a schematic's BOM for JLCPCB assembly.

    Args:
        schematic_path: Path to .kicad_sch file
        quantity: Number of boards
        use_cache: Whether to cache API responses

    Returns:
        AssemblyValidationResult with validation results
    """
    from kicad_tools.schema.bom import extract_bom

    bom = extract_bom(schematic_path)

    with AssemblyValidator(use_cache=use_cache) as validator:
        return validator.validate_bom(bom, quantity)

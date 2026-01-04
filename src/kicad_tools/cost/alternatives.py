"""
Alternative part suggestions for BOM components.

Suggests compatible replacement parts when originals are unavailable,
expensive, or have long lead times.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..parts.lcsc import LCSCClient
    from ..parts.models import Part, PartAvailability
    from ..schema.bom import BOMItem


@dataclass
class PartAlternative:
    """Alternative part suggestion."""

    # Original part info
    original_mpn: str
    original_lcsc: str | None

    # Alternative part info
    alternative_mpn: str
    alternative_lcsc: str
    alternative_manufacturer: str = ""
    alternative_description: str = ""

    # Compatibility assessment
    compatibility: str = "functional"  # "drop-in", "pin-compatible", "functional"
    differences: list[str] = field(default_factory=list)

    # Comparison metrics
    price_delta: float = 0.0  # Positive = more expensive
    original_price: float | None = None
    alternative_price: float | None = None
    stock_quantity: int = 0
    lead_time_days: int | None = None
    is_basic: bool = False

    # Recommendation
    recommendation: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "original": {
                "mpn": self.original_mpn,
                "lcsc": self.original_lcsc,
                "price": self.original_price,
            },
            "alternative": {
                "mpn": self.alternative_mpn,
                "lcsc": self.alternative_lcsc,
                "manufacturer": self.alternative_manufacturer,
                "description": self.alternative_description,
                "price": self.alternative_price,
            },
            "compatibility": self.compatibility,
            "differences": self.differences,
            "price_delta": round(self.price_delta, 4) if self.price_delta else 0,
            "stock_quantity": self.stock_quantity,
            "lead_time_days": self.lead_time_days,
            "is_basic": self.is_basic,
            "recommendation": self.recommendation,
            "warnings": self.warnings,
        }


@dataclass
class AlternativeSuggestions:
    """Collection of alternative suggestions for a BOM item."""

    reference: str
    value: str
    footprint: str
    original_lcsc: str | None
    original_mpn: str | None
    status: str  # "out_of_stock", "low_stock", "expensive", "long_lead_time"
    alternatives: list[PartAlternative] = field(default_factory=list)

    @property
    def has_alternatives(self) -> bool:
        return len(self.alternatives) > 0

    @property
    def best_alternative(self) -> PartAlternative | None:
        """Get the best alternative (first recommended one)."""
        for alt in self.alternatives:
            if alt.recommendation:
                return alt
        return self.alternatives[0] if self.alternatives else None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "reference": self.reference,
            "value": self.value,
            "footprint": self.footprint,
            "original_lcsc": self.original_lcsc,
            "original_mpn": self.original_mpn,
            "status": self.status,
            "alternatives": [alt.to_dict() for alt in self.alternatives],
        }


class AlternativePartFinder:
    """
    Find alternative parts for components with availability issues.

    Searches for compatible replacements when parts are:
    - Out of stock
    - Low stock
    - Discontinued
    - Expensive compared to alternatives

    Example::

        from kicad_tools.cost.alternatives import AlternativePartFinder
        from kicad_tools.parts import LCSCClient

        client = LCSCClient()
        finder = AlternativePartFinder(client)

        # Find alternatives for a specific part
        alternatives = finder.find_alternatives(bom_item)

        # Suggest alternatives for entire BOM
        suggestions = finder.suggest_for_bom(bom_items, availability_results)
    """

    # Common passive value patterns
    RESISTOR_VALUE_PATTERN = re.compile(
        r"(\d+(?:\.\d+)?)\s*([kKmMgG]?)\s*(?:ohm|Ω)?", re.IGNORECASE
    )
    CAPACITOR_VALUE_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*([pnuμmfF]?)\s*[Ff]?", re.IGNORECASE)
    INDUCTOR_VALUE_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*([pnuμmH]?)\s*[Hh]?", re.IGNORECASE)

    # Package size equivalents for passives
    PACKAGE_SIZES = ["0201", "0402", "0603", "0805", "1206", "1210", "2010", "2512"]

    def __init__(self, client: LCSCClient):
        """
        Initialize the alternative part finder.

        Args:
            client: LCSC client for searching parts
        """
        self.client = client

    def find_alternatives(
        self,
        item: BOMItem,
        max_results: int = 5,
        original_part: Part | None = None,
    ) -> list[PartAlternative]:
        """
        Find alternative parts for a component.

        Args:
            item: BOM item to find alternatives for
            max_results: Maximum number of alternatives to return
            original_part: Original Part object if already looked up

        Returns:
            List of alternative part suggestions
        """
        # Determine component type and find appropriate alternatives
        ref_prefix = self._get_reference_prefix(item.reference)

        if ref_prefix in ("R",):
            return self._find_resistor_alternatives(item, max_results, original_part)
        elif ref_prefix in ("C",):
            return self._find_capacitor_alternatives(item, max_results, original_part)
        elif ref_prefix in ("L",):
            return self._find_inductor_alternatives(item, max_results, original_part)
        elif ref_prefix in ("U",):
            return self._find_ic_alternatives(item, max_results, original_part)
        elif ref_prefix in ("D", "LED"):
            return self._find_diode_alternatives(item, max_results, original_part)
        elif ref_prefix in ("J", "P"):
            return self._find_connector_alternatives(item, max_results, original_part)
        elif ref_prefix in ("Q",):
            return self._find_transistor_alternatives(item, max_results, original_part)
        else:
            return self._find_generic_alternatives(item, max_results, original_part)

    def suggest_for_bom(
        self,
        items: list[BOMItem],
        availability: list[PartAvailability],
        max_results_per_item: int = 3,
    ) -> list[AlternativeSuggestions]:
        """
        Suggest alternatives for problematic BOM items.

        Args:
            items: List of BOM items
            availability: Availability results for those items
            max_results_per_item: Max alternatives per item

        Returns:
            List of suggestions for items needing alternatives
        """
        suggestions = []

        for item, avail in zip(items, availability, strict=True):
            # Determine if this item needs alternatives
            status = self._get_availability_status(avail)
            if status is None:
                continue

            # Find alternatives
            alternatives = self.find_alternatives(
                item, max_results=max_results_per_item, original_part=avail.part
            )

            if alternatives:
                suggestion = AlternativeSuggestions(
                    reference=item.reference,
                    value=item.value,
                    footprint=item.footprint,
                    original_lcsc=item.lcsc or None,
                    original_mpn=item.mpn or None,
                    status=status,
                    alternatives=alternatives,
                )
                suggestions.append(suggestion)

        return suggestions

    def _get_availability_status(self, avail: PartAvailability) -> str | None:
        """Determine if a part needs alternatives and why."""
        if avail.error:
            return "not_found"
        if not avail.matched:
            return "not_found"
        if not avail.in_stock:
            return "out_of_stock"
        if not avail.sufficient_stock:
            return "low_stock"
        return None

    def _get_reference_prefix(self, reference: str) -> str:
        """Extract reference designator prefix."""
        return "".join(c for c in reference if c.isalpha())

    def _find_resistor_alternatives(
        self, item: BOMItem, max_results: int, original_part: Part | None
    ) -> list[PartAlternative]:
        """Find alternative resistors."""
        # Parse value
        value = item.value
        package = self._extract_package_size(item.footprint)

        # Build search query
        query = f"{value} resistor {package}" if package else f"{value} resistor"

        return self._search_and_filter(
            query=query,
            item=item,
            original_part=original_part,
            max_results=max_results,
            compatibility_checker=self._check_resistor_compatibility,
        )

    def _find_capacitor_alternatives(
        self, item: BOMItem, max_results: int, original_part: Part | None
    ) -> list[PartAlternative]:
        """Find alternative capacitors."""
        value = item.value
        package = self._extract_package_size(item.footprint)

        query = f"{value} capacitor {package}" if package else f"{value} capacitor"

        return self._search_and_filter(
            query=query,
            item=item,
            original_part=original_part,
            max_results=max_results,
            compatibility_checker=self._check_capacitor_compatibility,
        )

    def _find_inductor_alternatives(
        self, item: BOMItem, max_results: int, original_part: Part | None
    ) -> list[PartAlternative]:
        """Find alternative inductors."""
        value = item.value
        package = self._extract_package_size(item.footprint)

        query = f"{value} inductor {package}" if package else f"{value} inductor"

        return self._search_and_filter(
            query=query,
            item=item,
            original_part=original_part,
            max_results=max_results,
            compatibility_checker=self._check_inductor_compatibility,
        )

    def _find_ic_alternatives(
        self, item: BOMItem, max_results: int, original_part: Part | None
    ) -> list[PartAlternative]:
        """Find alternative ICs."""
        # For ICs, use part family search
        mpn = item.mpn or item.value
        family = self._extract_part_family(mpn)

        if not family:
            return []

        return self._search_and_filter(
            query=family,
            item=item,
            original_part=original_part,
            max_results=max_results,
            compatibility_checker=self._check_ic_compatibility,
        )

    def _find_diode_alternatives(
        self, item: BOMItem, max_results: int, original_part: Part | None
    ) -> list[PartAlternative]:
        """Find alternative diodes/LEDs."""
        value = item.value
        package = self._extract_package_size(item.footprint)

        # Determine if LED or regular diode
        ref_prefix = self._get_reference_prefix(item.reference)
        part_type = "LED" if ref_prefix == "LED" or "LED" in value.upper() else "diode"

        query = f"{value} {part_type} {package}" if package else f"{value} {part_type}"

        return self._search_and_filter(
            query=query,
            item=item,
            original_part=original_part,
            max_results=max_results,
            compatibility_checker=self._check_diode_compatibility,
        )

    def _find_connector_alternatives(
        self, item: BOMItem, max_results: int, original_part: Part | None
    ) -> list[PartAlternative]:
        """Find alternative connectors."""
        # Connectors are very specific - search by MPN or description
        mpn = item.mpn or item.value

        return self._search_and_filter(
            query=mpn,
            item=item,
            original_part=original_part,
            max_results=max_results,
            compatibility_checker=self._check_connector_compatibility,
        )

    def _find_transistor_alternatives(
        self, item: BOMItem, max_results: int, original_part: Part | None
    ) -> list[PartAlternative]:
        """Find alternative transistors."""
        mpn = item.mpn or item.value
        package = self._extract_package_size(item.footprint)

        query = f"{mpn} {package}" if package else mpn

        return self._search_and_filter(
            query=query,
            item=item,
            original_part=original_part,
            max_results=max_results,
            compatibility_checker=self._check_transistor_compatibility,
        )

    def _find_generic_alternatives(
        self, item: BOMItem, max_results: int, original_part: Part | None
    ) -> list[PartAlternative]:
        """Find alternatives for generic components."""
        mpn = item.mpn or item.value

        return self._search_and_filter(
            query=mpn,
            item=item,
            original_part=original_part,
            max_results=max_results,
            compatibility_checker=self._check_generic_compatibility,
        )

    def _search_and_filter(
        self,
        query: str,
        item: BOMItem,
        original_part: Part | None,
        max_results: int,
        compatibility_checker,
    ) -> list[PartAlternative]:
        """Search for parts and filter to valid alternatives."""
        # Search for parts
        results = self.client.search(query, page_size=max_results * 3, in_stock=True)

        alternatives = []
        original_lcsc = (item.lcsc or "").upper()

        for part in results.parts:
            # Skip the original part
            if part.lcsc_part.upper() == original_lcsc:
                continue

            # Check compatibility
            compatibility, differences, warnings = compatibility_checker(item, part, original_part)

            if compatibility is None:
                continue  # Not compatible

            # Calculate price delta
            original_price = original_part.best_price if original_part else None
            alt_price = part.best_price
            price_delta = 0.0
            if original_price is not None and alt_price is not None:
                price_delta = alt_price - original_price

            # Determine recommendation
            recommendation = self._generate_recommendation(
                part, original_part, compatibility, differences
            )

            alt = PartAlternative(
                original_mpn=item.mpn or item.value,
                original_lcsc=item.lcsc or None,
                alternative_mpn=part.mfr_part,
                alternative_lcsc=part.lcsc_part,
                alternative_manufacturer=part.manufacturer,
                alternative_description=part.description,
                compatibility=compatibility,
                differences=differences,
                price_delta=price_delta,
                original_price=original_price,
                alternative_price=alt_price,
                stock_quantity=part.stock,
                lead_time_days=None,  # Would need additional API call
                is_basic=part.is_basic,
                recommendation=recommendation,
                warnings=warnings,
            )

            alternatives.append(alt)

            if len(alternatives) >= max_results:
                break

        # Sort by compatibility (drop-in first), then by price
        alternatives.sort(
            key=lambda a: (
                0
                if a.compatibility == "drop-in"
                else (1 if a.compatibility == "pin-compatible" else 2),
                a.price_delta or 0,
            )
        )

        return alternatives[:max_results]

    def _check_resistor_compatibility(
        self, item: BOMItem, part: Part, original: Part | None
    ) -> tuple[str | None, list[str], list[str]]:
        """Check resistor compatibility."""
        differences = []
        warnings = []

        # Check if same package
        item_package = self._extract_package_size(item.footprint)
        part_package = self._extract_package_size(part.package)

        if item_package and part_package:
            if item_package == part_package:
                compatibility = "drop-in"
            elif self._packages_compatible(item_package, part_package):
                compatibility = "pin-compatible"
                differences.append(f"Package: {item_package} → {part_package}")
            else:
                return None, [], []  # Incompatible package
        else:
            compatibility = "functional"
            warnings.append("Package compatibility uncertain")

        # Check value match from description
        if not self._values_match(item.value, part.description, "resistor"):
            return None, [], []  # Wrong value

        # Check tolerance if available
        if original and original.tolerance and part.tolerance:
            if original.tolerance != part.tolerance:
                differences.append(f"Tolerance: {original.tolerance} → {part.tolerance}")
                if self._tolerance_worse(original.tolerance, part.tolerance):
                    warnings.append("Lower precision than original")

        return compatibility, differences, warnings

    def _check_capacitor_compatibility(
        self, item: BOMItem, part: Part, original: Part | None
    ) -> tuple[str | None, list[str], list[str]]:
        """Check capacitor compatibility."""
        differences = []
        warnings = []

        item_package = self._extract_package_size(item.footprint)
        part_package = self._extract_package_size(part.package)

        if item_package and part_package:
            if item_package == part_package:
                compatibility = "drop-in"
            elif self._packages_compatible(item_package, part_package):
                compatibility = "pin-compatible"
                differences.append(f"Package: {item_package} → {part_package}")
            else:
                return None, [], []
        else:
            compatibility = "functional"
            warnings.append("Package compatibility uncertain")

        if not self._values_match(item.value, part.description, "capacitor"):
            return None, [], []

        # Check voltage rating
        if original and original.voltage_rating and part.voltage_rating:
            if original.voltage_rating != part.voltage_rating:
                differences.append(f"Voltage: {original.voltage_rating} → {part.voltage_rating}")
                if self._voltage_lower(original.voltage_rating, part.voltage_rating):
                    warnings.append("Lower voltage rating - verify circuit requirements")

        return compatibility, differences, warnings

    def _check_inductor_compatibility(
        self, item: BOMItem, part: Part, original: Part | None
    ) -> tuple[str | None, list[str], list[str]]:
        """Check inductor compatibility."""
        differences = []
        warnings = []

        item_package = self._extract_package_size(item.footprint)
        part_package = self._extract_package_size(part.package)

        if item_package and part_package:
            if item_package == part_package:
                compatibility = "drop-in"
            else:
                compatibility = "pin-compatible"
                differences.append(f"Package: {item_package} → {part_package}")
        else:
            compatibility = "functional"

        if not self._values_match(item.value, part.description, "inductor"):
            return None, [], []

        return compatibility, differences, warnings

    def _check_ic_compatibility(
        self, item: BOMItem, part: Part, original: Part | None
    ) -> tuple[str | None, list[str], list[str]]:
        """Check IC compatibility."""
        differences = []
        warnings = []

        # Check if same package
        item_package = item.footprint.split(":")[-1] if ":" in item.footprint else item.footprint
        part_package = part.package

        # For ICs, package must match exactly
        if item_package.lower() != part_package.lower():
            # Check for common equivalent packages
            if not self._ic_packages_compatible(item_package, part_package):
                return None, [], []
            differences.append(f"Package: {item_package} → {part_package}")
            warnings.append("Verify pinout compatibility")
            compatibility = "pin-compatible"
        else:
            compatibility = "drop-in"

        # Check part family
        original_mpn = item.mpn or item.value
        alt_mpn = part.mfr_part

        if not self._same_part_family(original_mpn, alt_mpn):
            return None, [], []

        # Note differences in variant
        if original_mpn != alt_mpn:
            differences.append(f"Variant: {original_mpn} → {alt_mpn}")

        return compatibility, differences, warnings

    def _check_diode_compatibility(
        self, item: BOMItem, part: Part, original: Part | None
    ) -> tuple[str | None, list[str], list[str]]:
        """Check diode/LED compatibility."""
        differences = []
        warnings = []

        item_package = self._extract_package_size(item.footprint)
        part_package = self._extract_package_size(part.package)

        if item_package and part_package and item_package == part_package:
            compatibility = "drop-in"
        else:
            compatibility = "functional"
            if item_package != part_package:
                differences.append(f"Package: {item_package} → {part_package}")
                warnings.append("Verify footprint compatibility")

        return compatibility, differences, warnings

    def _check_connector_compatibility(
        self, item: BOMItem, part: Part, original: Part | None
    ) -> tuple[str | None, list[str], list[str]]:
        """Check connector compatibility."""
        differences = []
        warnings = []

        # Connectors are very footprint-specific
        warnings.append("Verify exact footprint and pinout match")
        compatibility = "functional"

        return compatibility, differences, warnings

    def _check_transistor_compatibility(
        self, item: BOMItem, part: Part, original: Part | None
    ) -> tuple[str | None, list[str], list[str]]:
        """Check transistor compatibility."""
        differences = []
        warnings = []

        item_package = self._extract_package_size(item.footprint)
        part_package = self._extract_package_size(part.package)

        if item_package and part_package and item_package.lower() == part_package.lower():
            compatibility = "pin-compatible"
        else:
            compatibility = "functional"
            differences.append(f"Package: {item_package} → {part_package}")
            warnings.append("Verify pinout (different manufacturers may have different pinouts)")

        return compatibility, differences, warnings

    def _check_generic_compatibility(
        self, item: BOMItem, part: Part, original: Part | None
    ) -> tuple[str | None, list[str], list[str]]:
        """Generic compatibility check."""
        differences = []
        warnings = ["Manual verification required"]
        return "functional", differences, warnings

    def _extract_package_size(self, footprint: str) -> str | None:
        """Extract package size from footprint name."""
        # Common patterns: "0402", "0603", "SOT-23", etc.
        for size in self.PACKAGE_SIZES:
            if size in footprint:
                return size

        # Try to extract other package formats
        match = re.search(
            r"\b(SOT-\d+|SOIC-\d+|QFN-\d+|LQFP-\d+|TQFP-\d+)\b", footprint, re.IGNORECASE
        )
        if match:
            return match.group(1).upper()

        return None

    def _extract_part_family(self, mpn: str) -> str | None:
        """Extract part family from MPN for IC search."""
        if not mpn:
            return None

        # Common patterns:
        # STM32F103C8T6 -> STM32F103
        # ATmega328P-AU -> ATmega328
        # LM1117-3.3 -> LM1117

        # Remove common suffixes
        clean = re.sub(r"[-/].*$", "", mpn)  # Remove suffix after - or /
        clean = re.sub(r"[A-Z]{2,}$", "", clean)  # Remove 2+ letter suffix (like T6, AU)

        if len(clean) >= 4:
            return clean

        return mpn[:10] if len(mpn) > 10 else mpn

    def _packages_compatible(self, pkg1: str, pkg2: str) -> bool:
        """Check if two packages are pin-compatible."""
        # Same size passives are compatible
        if pkg1 in self.PACKAGE_SIZES and pkg2 in self.PACKAGE_SIZES:
            idx1 = self.PACKAGE_SIZES.index(pkg1)
            idx2 = self.PACKAGE_SIZES.index(pkg2)
            # Allow one size up or down
            return abs(idx1 - idx2) <= 1

        return False

    def _ic_packages_compatible(self, pkg1: str, pkg2: str) -> bool:
        """Check if two IC packages are compatible."""
        # Normalize package names
        p1 = pkg1.upper().replace("-", "").replace("_", "")
        p2 = pkg2.upper().replace("-", "").replace("_", "")

        # Check common equivalents
        equivalents = [
            ("SOIC8", "SOP8"),
            ("TSSOP20", "SSOP20"),
        ]

        for eq1, eq2 in equivalents:
            if (eq1 in p1 and eq2 in p2) or (eq2 in p1 and eq1 in p2):
                return True

        return p1 == p2

    def _same_part_family(self, mpn1: str, mpn2: str) -> bool:
        """Check if two MPNs are from the same part family."""
        family1 = self._extract_part_family(mpn1)
        family2 = self._extract_part_family(mpn2)

        if family1 and family2:
            # Check if one contains the other or they share a prefix
            return (
                family1 in family2
                or family2 in family1
                or family1[:6].lower() == family2[:6].lower()
            )

        return False

    def _values_match(self, item_value: str, part_description: str, part_type: str) -> bool:
        """Check if the part value matches."""
        # Normalize values
        item_val = item_value.lower().strip()
        desc = part_description.lower()

        # Direct match
        if item_val in desc:
            return True

        # Try normalized value match
        if part_type == "resistor":
            norm_item = self._normalize_resistor_value(item_value)
            norm_desc = self._normalize_resistor_value(part_description)
            if norm_item and norm_desc and norm_item == norm_desc:
                return True
        elif part_type == "capacitor":
            norm_item = self._normalize_capacitor_value(item_value)
            norm_desc = self._normalize_capacitor_value(part_description)
            if norm_item and norm_desc and norm_item == norm_desc:
                return True

        return False

    def _normalize_resistor_value(self, value: str) -> float | None:
        """Normalize resistor value to ohms."""
        match = self.RESISTOR_VALUE_PATTERN.search(value)
        if not match:
            return None

        num = float(match.group(1))
        mult = match.group(2).lower() if match.group(2) else ""

        multipliers = {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}
        return num * multipliers.get(mult, 1)

    def _normalize_capacitor_value(self, value: str) -> float | None:
        """Normalize capacitor value to farads."""
        match = self.CAPACITOR_VALUE_PATTERN.search(value)
        if not match:
            return None

        num = float(match.group(1))
        mult = match.group(2).lower() if match.group(2) else ""

        multipliers = {"": 1, "f": 1, "m": 1e-3, "u": 1e-6, "μ": 1e-6, "n": 1e-9, "p": 1e-12}
        return num * multipliers.get(mult, 1)

    def _tolerance_worse(self, orig: str, alt: str) -> bool:
        """Check if alternative tolerance is worse (higher percentage)."""
        try:
            orig_pct = float(orig.replace("%", ""))
            alt_pct = float(alt.replace("%", ""))
            return alt_pct > orig_pct
        except ValueError:
            return False

    def _voltage_lower(self, orig: str, alt: str) -> bool:
        """Check if alternative voltage is lower."""
        try:
            orig_v = float(re.search(r"(\d+(?:\.\d+)?)", orig).group(1))
            alt_v = float(re.search(r"(\d+(?:\.\d+)?)", alt).group(1))
            return alt_v < orig_v
        except (ValueError, AttributeError):
            return False

    def _generate_recommendation(
        self,
        part: Part,
        original: Part | None,
        compatibility: str,
        differences: list[str],
    ) -> str:
        """Generate recommendation text for an alternative."""
        reasons = []

        if part.is_basic:
            reasons.append("JLCPCB basic part (no extended fee)")

        if part.stock > 10000:
            reasons.append(f"high stock ({part.stock:,})")

        if original and part.best_price and original.best_price:
            if part.best_price < original.best_price:
                savings = original.best_price - part.best_price
                reasons.append(f"${savings:.4f}/unit cheaper")

        if compatibility == "drop-in":
            reasons.append("drop-in replacement")

        if not reasons:
            return ""

        return "Recommended: " + ", ".join(reasons)

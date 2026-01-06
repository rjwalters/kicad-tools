"""
Part suggestion engine for auto-populating LCSC part numbers.

Analyzes component values and footprints to suggest matching LCSC parts.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..parts.models import Part
    from ..schema.bom import BOM

logger = logging.getLogger(__name__)


class ComponentType(Enum):
    """Component type classification."""

    RESISTOR = "resistor"
    CAPACITOR = "capacitor"
    INDUCTOR = "inductor"
    LED = "led"
    DIODE = "diode"
    TRANSISTOR = "transistor"
    IC = "ic"
    CONNECTOR = "connector"
    CRYSTAL = "crystal"
    FUSE = "fuse"
    OTHER = "other"


@dataclass
class ParsedValue:
    """Parsed component value."""

    raw_value: str
    component_type: ComponentType
    numeric_value: float | None = None
    unit: str = ""
    tolerance: str = ""
    voltage_rating: str = ""
    search_terms: list[str] = field(default_factory=list)


@dataclass
class SuggestedPart:
    """A suggested LCSC part for a component."""

    lcsc_part: str
    mfr_part: str
    description: str
    package: str
    stock: int
    is_basic: bool
    is_preferred: bool
    unit_price: float | None
    confidence: float  # 0.0 to 1.0

    @property
    def type_str(self) -> str:
        """Get part type string for display."""
        if self.is_basic:
            return "Basic"
        elif self.is_preferred:
            return "Pref"
        else:
            return "Ext"


@dataclass
class PartSuggestion:
    """Suggestion result for a BOM item."""

    # Component info
    reference: str
    value: str
    footprint: str
    package: str  # Extracted package size

    # Existing LCSC (if any)
    existing_lcsc: str | None

    # Suggestions
    suggestions: list[SuggestedPart] = field(default_factory=list)
    best_suggestion: SuggestedPart | None = None

    # Status
    search_query: str = ""
    error: str | None = None

    @property
    def has_suggestion(self) -> bool:
        """Check if any suggestion was found."""
        return self.best_suggestion is not None

    @property
    def needs_lcsc(self) -> bool:
        """Check if this component needs an LCSC number."""
        return not self.existing_lcsc


@dataclass
class SuggestionResult:
    """Results from suggesting parts for a BOM."""

    suggestions: list[PartSuggestion] = field(default_factory=list)

    @property
    def total_components(self) -> int:
        """Total number of components analyzed."""
        return len(self.suggestions)

    @property
    def missing_lcsc(self) -> int:
        """Components without LCSC numbers."""
        return len([s for s in self.suggestions if s.needs_lcsc])

    @property
    def found_suggestions(self) -> int:
        """Components with suggestions found."""
        return len([s for s in self.suggestions if s.has_suggestion])

    @property
    def no_suggestions(self) -> int:
        """Components without any suggestions."""
        return len([s for s in self.suggestions if not s.has_suggestion and s.needs_lcsc])


# Value parsing patterns
RESISTOR_PATTERN = re.compile(
    r"^(\d+(?:\.\d+)?)\s*([kKmMgG]?)\s*([Ωohm]*)(?:\s+(\d+(?:\.\d+)?%?))?$", re.IGNORECASE
)
CAPACITOR_PATTERN = re.compile(
    r"^(\d+(?:\.\d+)?)\s*([pnuμmµ]?)[fF]?(?:\s+(\d+[Vv]))?(?:\s+(\d+(?:\.\d+)?%?))?$",
    re.IGNORECASE,
)
INDUCTOR_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)\s*([pnuμmµ]?)[hH]?$", re.IGNORECASE)

# Common footprint to package mappings
FOOTPRINT_PACKAGE_MAP = {
    # Metric chip packages (with various naming conventions)
    "0201": "0201",
    "0402": "0402",
    "0603": "0603",
    "0805": "0805",
    "1206": "1206",
    "1210": "1210",
    "1812": "1812",
    "2010": "2010",
    "2512": "2512",
    # Imperial to metric mappings
    "01005": "01005",
    # Common KiCad footprint patterns
    "c_0201": "0201",
    "c_0402": "0402",
    "c_0603": "0603",
    "c_0805": "0805",
    "c_1206": "1206",
    "r_0201": "0201",
    "r_0402": "0402",
    "r_0603": "0603",
    "r_0805": "0805",
    "r_1206": "1206",
    "l_0402": "0402",
    "l_0603": "0603",
    "l_0805": "0805",
    # SOT packages
    "sot-23": "SOT-23",
    "sot-223": "SOT-223",
    "sot-89": "SOT-89",
    "sot23": "SOT-23",
    "sot223": "SOT-223",
    "sot89": "SOT-89",
    # QFP/TQFP/LQFP
    "tqfp-32": "TQFP-32",
    "tqfp-44": "TQFP-44",
    "tqfp-48": "TQFP-48",
    "tqfp-64": "TQFP-64",
    "tqfp-100": "TQFP-100",
    "lqfp-32": "LQFP-32",
    "lqfp-48": "LQFP-48",
    "lqfp-64": "LQFP-64",
    "lqfp-100": "LQFP-100",
    # SOIC
    "soic-8": "SOIC-8",
    "soic-14": "SOIC-14",
    "soic-16": "SOIC-16",
    # TSSOP (before SSOP to match more specific pattern first)
    "tssop-8": "TSSOP-8",
    "tssop-14": "TSSOP-14",
    "tssop-16": "TSSOP-16",
    "tssop-20": "TSSOP-20",
    # SSOP
    "ssop-8": "SSOP-8",
    "ssop-16": "SSOP-16",
    "ssop-20": "SSOP-20",
    # QFN/DFN
    "qfn-16": "QFN-16",
    "qfn-20": "QFN-20",
    "qfn-24": "QFN-24",
    "qfn-32": "QFN-32",
    "dfn-8": "DFN-8",
    # TO packages
    "to-92": "TO-92",
    "to-220": "TO-220",
    "to-252": "TO-252",
    "to-263": "TO-263",
    "dpak": "TO-252",
    "d2pak": "TO-263",
}


def extract_package_from_footprint(footprint: str) -> str:
    """
    Extract package size from KiCad footprint name.

    Examples:
        "Capacitor_SMD:C_0402_1005Metric" -> "0402"
        "Resistor_SMD:R_0805_2012Metric" -> "0805"
        "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm" -> "SOIC-8"

    Args:
        footprint: KiCad footprint string

    Returns:
        Extracted package name or empty string
    """
    if not footprint:
        return ""

    # Get the footprint name after the library prefix
    if ":" in footprint:
        footprint = footprint.split(":")[-1]

    footprint_lower = footprint.lower()

    # Try direct mapping first
    for pattern, package in FOOTPRINT_PACKAGE_MAP.items():
        if pattern in footprint_lower:
            return package

    # Extract 4-digit chip size pattern (e.g., 0402, 0805)
    chip_match = re.search(r"[_-]?(\d{4})[_-]?", footprint)
    if chip_match:
        size = chip_match.group(1)
        if size in FOOTPRINT_PACKAGE_MAP:
            return FOOTPRINT_PACKAGE_MAP[size]
        return size

    # Extract package with pin count (e.g., SOIC-8, TQFP-32)
    # Note: longer patterns must come first (tssop before ssop)
    pkg_match = re.search(r"(tssop|tqfp|lqfp|soic|ssop|qfn|dfn|qfp|sot)-?(\d+)", footprint_lower)
    if pkg_match:
        pkg_type = pkg_match.group(1).upper()
        pin_count = pkg_match.group(2)
        return f"{pkg_type}-{pin_count}"

    return ""


def parse_component_value(value: str, reference: str = "") -> ParsedValue:
    """
    Parse a component value string to extract searchable information.

    Args:
        value: Component value (e.g., "100nF", "10k", "STM32C011F4P6")
        reference: Reference designator for type hints (e.g., "R1", "C1")

    Returns:
        ParsedValue with extracted information
    """
    value = value.strip()

    # Determine component type from reference prefix
    ref_prefix = reference[:1].upper() if reference else ""
    component_type = ComponentType.OTHER

    if ref_prefix == "R":
        component_type = ComponentType.RESISTOR
    elif ref_prefix == "C":
        component_type = ComponentType.CAPACITOR
    elif ref_prefix == "L":
        component_type = ComponentType.INDUCTOR
    elif ref_prefix == "D":
        if "LED" in value.upper():
            component_type = ComponentType.LED
        else:
            component_type = ComponentType.DIODE
    elif ref_prefix == "Q":
        component_type = ComponentType.TRANSISTOR
    elif ref_prefix == "U":
        component_type = ComponentType.IC
    elif ref_prefix == "J" or ref_prefix == "P":
        component_type = ComponentType.CONNECTOR
    elif ref_prefix == "Y" or ref_prefix == "X":
        component_type = ComponentType.CRYSTAL
    elif ref_prefix == "F":
        component_type = ComponentType.FUSE

    parsed = ParsedValue(
        raw_value=value,
        component_type=component_type,
        search_terms=[],
    )

    # Parse resistor values
    if component_type == ComponentType.RESISTOR:
        match = RESISTOR_PATTERN.match(value)
        if match:
            num = float(match.group(1))
            multiplier = match.group(2).upper() if match.group(2) else ""

            if multiplier == "K":
                num *= 1000
            elif multiplier == "M":
                num *= 1_000_000
            elif multiplier == "G":
                num *= 1_000_000_000

            parsed.numeric_value = num
            parsed.unit = "Ω"

            if match.group(4):
                parsed.tolerance = match.group(4)

            # Build search terms
            if num >= 1_000_000:
                parsed.search_terms.append(f"{num / 1_000_000:.3g}M")
            elif num >= 1000:
                parsed.search_terms.append(f"{num / 1000:.3g}k")
            else:
                parsed.search_terms.append(f"{num:.3g}")

    # Parse capacitor values
    elif component_type == ComponentType.CAPACITOR:
        match = CAPACITOR_PATTERN.match(value)
        if match:
            num = float(match.group(1))
            prefix = match.group(2).lower() if match.group(2) else ""

            # Convert to farads
            multipliers = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "μ": 1e-6, "µ": 1e-6, "m": 1e-3}
            if prefix in multipliers:
                num *= multipliers[prefix]

            parsed.numeric_value = num
            parsed.unit = "F"

            if match.group(3):
                parsed.voltage_rating = match.group(3)
            if match.group(4):
                parsed.tolerance = match.group(4)

            # Build search terms
            if num >= 1e-6:
                parsed.search_terms.append(f"{num * 1e6:.3g}uF")
            elif num >= 1e-9:
                parsed.search_terms.append(f"{num * 1e9:.3g}nF")
            elif num >= 1e-12:
                parsed.search_terms.append(f"{num * 1e12:.3g}pF")

    # Parse inductor values
    elif component_type == ComponentType.INDUCTOR:
        match = INDUCTOR_PATTERN.match(value)
        if match:
            num = float(match.group(1))
            prefix = match.group(2).lower() if match.group(2) else ""

            multipliers = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "μ": 1e-6, "µ": 1e-6, "m": 1e-3}
            if prefix in multipliers:
                num *= multipliers[prefix]

            parsed.numeric_value = num
            parsed.unit = "H"

            # Build search terms
            if num >= 1e-6:
                parsed.search_terms.append(f"{num * 1e6:.3g}uH")
            elif num >= 1e-9:
                parsed.search_terms.append(f"{num * 1e9:.3g}nH")
            elif num >= 1e-12:
                parsed.search_terms.append(f"{num * 1e12:.3g}pH")

    # For ICs and other components, use the value directly
    else:
        parsed.search_terms.append(value)

    return parsed


class PartSuggester:
    """
    Suggests LCSC parts based on component values and footprints.

    Example::

        suggester = PartSuggester()
        result = suggester.suggest_for_bom(bom)

        for suggestion in result.suggestions:
            if suggestion.has_suggestion:
                print(f"{suggestion.reference}: {suggestion.best_suggestion.lcsc_part}")
    """

    def __init__(
        self,
        prefer_basic: bool = True,
        min_stock: int = 100,
        max_suggestions: int = 3,
    ):
        """
        Initialize the suggester.

        Args:
            prefer_basic: Prefer JLCPCB Basic parts (default: True)
            min_stock: Minimum stock level to consider (default: 100)
            max_suggestions: Maximum suggestions per component (default: 3)
        """
        self.prefer_basic = prefer_basic
        self.min_stock = min_stock
        self.max_suggestions = max_suggestions
        self._client = None

    def _get_client(self):
        """Get or create LCSC client."""
        if self._client is None:
            from ..parts import LCSCClient

            self._client = LCSCClient()
        return self._client

    def suggest_for_bom(self, bom: BOM) -> SuggestionResult:
        """
        Suggest parts for all components in a BOM.

        Args:
            bom: Bill of Materials to analyze

        Returns:
            SuggestionResult with suggestions for each component
        """
        suggestions = []
        groups = bom.grouped()

        for group in groups:
            # Skip DNP items and items that already have LCSC numbers
            if group.items and group.items[0].dnp:
                continue

            # Get first item for reference info
            first_item = group.items[0]

            suggestion = self.suggest_for_component(
                reference=first_item.reference,
                value=group.value,
                footprint=group.footprint,
                existing_lcsc=group.lcsc or None,
            )
            suggestions.append(suggestion)

        return SuggestionResult(suggestions=suggestions)

    def suggest_for_component(
        self,
        reference: str,
        value: str,
        footprint: str,
        existing_lcsc: str | None = None,
    ) -> PartSuggestion:
        """
        Suggest LCSC parts for a single component.

        Args:
            reference: Reference designator (e.g., "R1")
            value: Component value (e.g., "10k")
            footprint: KiCad footprint string
            existing_lcsc: Existing LCSC number if any

        Returns:
            PartSuggestion with best matches
        """
        package = extract_package_from_footprint(footprint)
        parsed = parse_component_value(value, reference)

        suggestion = PartSuggestion(
            reference=reference,
            value=value,
            footprint=footprint,
            package=package,
            existing_lcsc=existing_lcsc,
        )

        # Skip if already has LCSC
        if existing_lcsc:
            return suggestion

        # Build search query
        search_terms = parsed.search_terms.copy()
        if package:
            search_terms.append(package)

        if not search_terms:
            suggestion.error = "Unable to parse value for search"
            return suggestion

        query = " ".join(search_terms)
        suggestion.search_query = query

        # Search LCSC
        try:
            client = self._get_client()
            results = client.search(
                query,
                in_stock=True,
                page_size=20,
            )

            # Filter and rank results
            candidates = []
            for part in results.parts:
                # Skip parts with insufficient stock
                if part.stock < self.min_stock:
                    continue

                # Calculate confidence score
                confidence = self._calculate_confidence(part, parsed, package)

                candidates.append(
                    SuggestedPart(
                        lcsc_part=part.lcsc_part,
                        mfr_part=part.mfr_part,
                        description=part.description,
                        package=part.package,
                        stock=part.stock,
                        is_basic=part.is_basic,
                        is_preferred=part.is_preferred,
                        unit_price=part.best_price,
                        confidence=confidence,
                    )
                )

            # Sort by: basic > preferred > extended, then by confidence
            def sort_key(p: SuggestedPart):
                type_score = 0 if p.is_basic else (1 if p.is_preferred else 2)
                if not self.prefer_basic:
                    type_score = 0  # Don't prioritize by type
                return (type_score, -p.confidence, -p.stock)

            candidates.sort(key=sort_key)

            # Take top suggestions
            suggestion.suggestions = candidates[: self.max_suggestions]
            if suggestion.suggestions:
                suggestion.best_suggestion = suggestion.suggestions[0]

        except Exception as e:
            logger.warning(f"Search failed for {reference}: {e}")
            suggestion.error = str(e)

        return suggestion

    def _calculate_confidence(
        self,
        part: Part,
        parsed: ParsedValue,
        target_package: str,
    ) -> float:
        """Calculate confidence score for a part match."""
        confidence = 0.5  # Base confidence

        # Package match is important
        if target_package:
            if target_package.lower() in part.package.lower():
                confidence += 0.3
            elif part.package.lower() in target_package.lower():
                confidence += 0.2

        # Value match
        if parsed.search_terms:
            desc_lower = part.description.lower()
            for term in parsed.search_terms:
                if term.lower() in desc_lower:
                    confidence += 0.1

        # Prefer parts with good stock
        if part.stock > 10000:
            confidence += 0.1
        elif part.stock > 1000:
            confidence += 0.05

        return min(confidence, 1.0)

    def close(self) -> None:
        """Close the LCSC client."""
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

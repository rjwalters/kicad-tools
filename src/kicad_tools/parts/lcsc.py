"""
LCSC/JLCPCB parts client.

Fetches parts data from JLCPCB/LCSC for assembly service integration.
Uses the same API endpoints as the KiCad JLCPCB Plugin.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional
from urllib.parse import quote

from .cache import PartsCache
from .models import (
    BOMAvailability,
    PackageType,
    Part,
    PartAvailability,
    PartCategory,
    PartPrice,
    SearchResult,
)

if TYPE_CHECKING:
    from ..schema.bom import BOMItem

logger = logging.getLogger(__name__)


# JLCPCB API endpoints (same as KiCad JLCPCB Plugin)
JLCPCB_API_BASE = "https://jlcpcb.com/api"
LCSC_API_BASE = "https://wmsc.lcsc.com/ftps/wm"

# Part lookup endpoint
PART_LOOKUP_URL = f"{JLCPCB_API_BASE}/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentDetail"

# Search endpoint
SEARCH_URL = f"{JLCPCB_API_BASE}/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList"


def _requires_requests(func):
    """Decorator to check if requests is available."""
    def wrapper(*args, **kwargs):
        try:
            import requests  # noqa: F401
        except ImportError:
            raise ImportError(
                "The 'requests' library is required for LCSC API access. "
                "Install with: pip install kicad-tools[parts]"
            )
        return func(*args, **kwargs)
    return wrapper


def _categorize_part(description: str, package: str) -> PartCategory:
    """Guess category from description and package."""
    desc_lower = description.lower()

    if any(x in desc_lower for x in ["resistor", "res ", " res", "ohm"]):
        return PartCategory.RESISTOR
    if any(x in desc_lower for x in ["capacitor", "cap ", " cap", "farad", "mlcc"]):
        return PartCategory.CAPACITOR
    if any(x in desc_lower for x in ["inductor", "ind ", " ind", "henry", "choke"]):
        return PartCategory.INDUCTOR
    if any(x in desc_lower for x in ["diode", "rectifier", "schottky", "zener"]):
        return PartCategory.DIODE
    if any(x in desc_lower for x in ["transistor", "mosfet", "bjt", "jfet"]):
        return PartCategory.TRANSISTOR
    if any(x in desc_lower for x in ["mcu", "microcontroller", "op amp", "opamp", "regulator", "eeprom", "flash"]):
        return PartCategory.IC
    if any(x in desc_lower for x in ["connector", "header", "socket", "jack", "plug"]):
        return PartCategory.CONNECTOR
    if any(x in desc_lower for x in ["crystal", "oscillator", "resonator"]):
        return PartCategory.CRYSTAL
    if any(x in desc_lower for x in ["led", "light emitting"]):
        return PartCategory.LED
    if any(x in desc_lower for x in ["switch", "button", "tactile"]):
        return PartCategory.SWITCH
    if any(x in desc_lower for x in ["relay"]):
        return PartCategory.RELAY
    if any(x in desc_lower for x in ["fuse", "ptc", "polyfuse"]):
        return PartCategory.FUSE

    return PartCategory.OTHER


def _guess_package_type(package: str) -> PackageType:
    """Guess package type (SMD vs through-hole)."""
    package_lower = package.lower()

    # Common SMD packages
    smd_patterns = [
        r"^\d{4}$",  # 0402, 0603, 0805, etc.
        r"^\d{4}_\d{4}",  # 0402_1005Metric
        r"^smd",
        r"^smt",
        r"^soic",
        r"^sop",
        r"^ssop",
        r"^tssop",
        r"^qfp",
        r"^lqfp",
        r"^tqfp",
        r"^qfn",
        r"^dfn",
        r"^bga",
        r"^sot",
        r"^sc-",
        r"^to-252",
        r"^to-263",
        r"^dpak",
        r"^d2pak",
    ]

    for pattern in smd_patterns:
        if re.search(pattern, package_lower):
            return PackageType.SMD

    # Common through-hole packages
    th_patterns = [
        r"^dip",
        r"^pdip",
        r"^to-92",
        r"^to-220",
        r"^to-247",
        r"^axial",
        r"^radial",
        r"^through",
    ]

    for pattern in th_patterns:
        if re.search(pattern, package_lower):
            return PackageType.THROUGH_HOLE

    return PackageType.UNKNOWN


class LCSCClient:
    """
    Client for LCSC/JLCPCB parts API.

    Provides methods for looking up parts by LCSC number and searching
    the parts catalog. Results are cached locally to reduce API calls.

    Example::

        client = LCSCClient()

        # Single part lookup
        part = client.lookup("C123456")
        if part:
            print(f"{part.mfr_part}: ${part.best_price:.4f}")

        # Search for parts
        results = client.search("100nF 0402")
        for part in results:
            print(f"{part.lcsc_part}: {part.description}")

        # Check BOM availability
        from kicad_tools.schema.bom import extract_bom
        bom = extract_bom("project.kicad_sch")
        availability = client.check_bom(bom.items)

    Note:
        Requires the `requests` library. Install with::

            pip install kicad-tools[parts]
    """

    # Default headers matching browser requests
    DEFAULT_HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": "https://jlcpcb.com",
        "Referer": "https://jlcpcb.com/parts",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }

    def __init__(
        self,
        cache: Optional[PartsCache] = None,
        use_cache: bool = True,
        timeout: float = 30.0,
    ):
        """
        Initialize the client.

        Args:
            cache: Custom cache instance (default: creates new PartsCache)
            use_cache: Whether to use caching (default: True)
            timeout: Request timeout in seconds
        """
        self.cache = cache if cache is not None else PartsCache() if use_cache else None
        self.timeout = timeout
        self._session = None

    def _get_session(self):
        """Get or create requests session."""
        if self._session is None:
            import requests
            self._session = requests.Session()
            self._session.headers.update(self.DEFAULT_HEADERS)
        return self._session

    @_requires_requests
    def lookup(self, lcsc_part: str, bypass_cache: bool = False) -> Optional[Part]:
        """
        Look up a single part by LCSC number.

        Args:
            lcsc_part: LCSC part number (e.g., "C123456")
            bypass_cache: If True, always fetch from API

        Returns:
            Part if found, None otherwise
        """
        # Normalize part number
        lcsc_part = lcsc_part.upper()
        if not lcsc_part.startswith("C"):
            lcsc_part = f"C{lcsc_part}"

        # Check cache first
        if self.cache and not bypass_cache:
            cached = self.cache.get(lcsc_part)
            if cached:
                logger.debug(f"Cache hit for {lcsc_part}")
                return cached

        # Fetch from API
        try:
            part = self._fetch_part(lcsc_part)
            if part and self.cache:
                self.cache.put(part)
            return part
        except Exception as e:
            logger.error(f"Failed to lookup {lcsc_part}: {e}")
            return None

    def _fetch_part(self, lcsc_part: str) -> Optional[Part]:
        """Fetch part from JLCPCB API."""
        import requests

        session = self._get_session()

        payload = {
            "componentCode": lcsc_part,
        }

        try:
            response = session.post(
                PART_LOOKUP_URL,
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            logger.warning(f"API request failed for {lcsc_part}: {e}")
            return None

        # Check for success
        if data.get("code") != 200:
            logger.debug(f"API returned error for {lcsc_part}: {data.get('message')}")
            return None

        component = data.get("data")
        if not component:
            return None

        return self._parse_component(component)

    def _parse_component(self, data: dict) -> Part:
        """Parse component data from API response."""
        # Extract price breaks
        prices = []
        price_list = data.get("prices") or data.get("priceList") or []
        for price_break in price_list:
            qty = price_break.get("startNumber", 0)
            unit_price = price_break.get("productPrice", 0)
            if qty > 0 and unit_price > 0:
                prices.append(PartPrice(quantity=qty, unit_price=unit_price))

        # Sort by quantity
        prices.sort(key=lambda p: p.quantity)

        # Get package info
        package = data.get("encapStandard") or data.get("package") or ""
        package_type = _guess_package_type(package)

        # Get description
        description = data.get("componentModelEn") or data.get("describe") or ""

        # Categorize
        category = _categorize_part(description, package)

        return Part(
            lcsc_part=data.get("componentCode", ""),
            mfr_part=data.get("componentModelEn") or data.get("manufacturerPartNumber") or "",
            manufacturer=data.get("componentBrandEn") or data.get("manufacturer") or "",
            description=description,
            category=category,
            package=package,
            package_type=package_type,
            stock=data.get("stockCount", 0),
            min_order=data.get("minOrder", 1),
            prices=prices,
            is_basic=data.get("componentLibraryType") == "base",
            is_preferred=data.get("componentLibraryType") == "preferred",
            datasheet_url=data.get("dataManualUrl") or "",
            product_url=f"https://jlcpcb.com/partdetail/{data.get('componentCode', '')}",
            fetched_at=datetime.now(),
        )

    @_requires_requests
    def search(
        self,
        query: str,
        page: int = 1,
        page_size: int = 20,
        in_stock: bool = False,
        basic_only: bool = False,
    ) -> SearchResult:
        """
        Search for parts.

        Args:
            query: Search query string
            page: Page number (1-indexed)
            page_size: Results per page (max 100)
            in_stock: Only return in-stock parts
            basic_only: Only return JLCPCB basic parts

        Returns:
            SearchResult with matching parts
        """
        import requests

        session = self._get_session()

        payload = {
            "keyword": query,
            "pageSize": min(page_size, 100),
            "currentPage": page,
        }

        if in_stock:
            payload["stockCountMin"] = 1

        if basic_only:
            payload["componentLibraryType"] = "base"

        try:
            response = session.post(
                SEARCH_URL,
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            logger.error(f"Search request failed: {e}")
            return SearchResult(query=query)

        if data.get("code") != 200:
            logger.warning(f"Search API returned error: {data.get('message')}")
            return SearchResult(query=query)

        result_data = data.get("data", {})
        components = result_data.get("componentPageInfo", {}).get("list", [])
        total = result_data.get("componentPageInfo", {}).get("total", 0)

        parts = []
        for comp in components:
            try:
                part = self._parse_component(comp)
                parts.append(part)
                # Cache search results
                if self.cache:
                    self.cache.put(part)
            except Exception as e:
                logger.warning(f"Failed to parse component: {e}")
                continue

        return SearchResult(
            query=query,
            parts=parts,
            total_count=total,
            page=page,
            page_size=page_size,
        )

    @_requires_requests
    def lookup_many(
        self,
        lcsc_parts: List[str],
        bypass_cache: bool = False,
    ) -> dict[str, Part]:
        """
        Look up multiple parts.

        Uses cache where possible, fetches missing parts from API.

        Args:
            lcsc_parts: List of LCSC part numbers
            bypass_cache: If True, always fetch from API

        Returns:
            Dict mapping part numbers to Parts
        """
        if not lcsc_parts:
            return {}

        # Normalize part numbers
        parts = [p.upper() if p.upper().startswith("C") else f"C{p.upper()}" for p in lcsc_parts]

        result = {}

        # Check cache first
        if self.cache and not bypass_cache:
            cached = self.cache.get_many(parts)
            result.update(cached)
            parts = [p for p in parts if p not in cached]

        # Fetch remaining from API
        for part_num in parts:
            part = self._fetch_part(part_num)
            if part:
                result[part_num] = part
                if self.cache:
                    self.cache.put(part)

        return result

    def check_bom(
        self,
        items: List["BOMItem"],
        bypass_cache: bool = False,
    ) -> BOMAvailability:
        """
        Check availability for BOM items.

        Args:
            items: List of BOM items (must have .lcsc field)
            bypass_cache: If True, always fetch from API

        Returns:
            BOMAvailability with check results
        """
        # Collect LCSC part numbers from BOM
        lcsc_parts = []
        for item in items:
            if hasattr(item, "lcsc") and item.lcsc:
                lcsc_parts.append(item.lcsc)

        # Fetch all parts
        parts_map = self.lookup_many(list(set(lcsc_parts)), bypass_cache=bypass_cache)

        # Build availability results
        results = []
        for item in items:
            lcsc = getattr(item, "lcsc", None) or ""

            avail = PartAvailability(
                reference=item.reference,
                value=item.value,
                footprint=item.footprint,
                lcsc_part=lcsc,
                quantity_needed=item.quantity,
            )

            if not lcsc:
                avail.error = "No LCSC part number"
            elif lcsc.upper() in parts_map:
                part = parts_map[lcsc.upper()]
                avail.part = part
                avail.matched = True
                avail.in_stock = part.in_stock
                avail.quantity_available = part.stock
            else:
                avail.error = "Part not found"

            results.append(avail)

        return BOMAvailability(
            items=results,
            checked_at=datetime.now(),
        )

    def close(self) -> None:
        """Close the HTTP session."""
        if self._session:
            self._session.close()
            self._session = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

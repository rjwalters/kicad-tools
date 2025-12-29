"""
Parts database and supplier integration.

Provides access to LCSC/JLCPCB parts catalog for:
- Part lookup by LCSC number
- Parts search
- BOM availability checking
- Local caching for offline use

Example::

    from kicad_tools.parts import LCSCClient

    # Create client
    client = LCSCClient()

    # Look up a single part
    part = client.lookup("C123456")
    if part:
        print(f"{part.mfr_part}: ${part.best_price:.4f}")
        print(f"In stock: {part.stock}")

    # Search for parts
    results = client.search("100nF 0402", in_stock=True)
    for part in results:
        print(f"{part.lcsc_part}: {part.description}")

    # Check BOM availability
    from kicad_tools.schema.bom import extract_bom
    bom = extract_bom("project.kicad_sch")
    availability = client.check_bom(bom.items)

    print(f"Available: {len(availability.available)}")
    print(f"Missing: {len(availability.missing_parts)}")

Note:
    LCSC API access requires the `requests` library.
    Install with: pip install kicad-tools[parts]
"""

from .cache import PartsCache, get_default_cache_path
from .lcsc import LCSCClient
from .models import (
    BOMAvailability,
    PackageType,
    Part,
    PartAvailability,
    PartCategory,
    PartPrice,
    SearchResult,
)

__all__ = [
    # Client
    "LCSCClient",
    # Cache
    "PartsCache",
    "get_default_cache_path",
    # Models
    "Part",
    "PartPrice",
    "PartCategory",
    "PackageType",
    "PartAvailability",
    "BOMAvailability",
    "SearchResult",
]

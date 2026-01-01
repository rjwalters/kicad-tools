"""
Parts database and supplier integration.

Provides access to LCSC/JLCPCB parts catalog for:
- Part lookup by LCSC number
- Parts search
- BOM availability checking
- Local caching for offline use
- End-to-end part import workflow

Example - Parts Lookup::

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

Example - Part Import::

    from kicad_tools.parts import PartImporter

    importer = PartImporter(
        symbol_library="myproject.kicad_sym",
        footprint_library="MyProject.pretty",
    )

    # Import single part
    result = importer.import_part("STM32F103C8T6")
    print(f"Symbol: {result.symbol_name}")
    print(f"Footprint: {result.footprint_match}")

    # Batch import
    results = importer.import_parts(["STM32F103C8T6", "ATmega328P"])
    for r in results:
        print(f"{r.part_number}: {'✓' if r.success else '✗'} {r.message}")

Note:
    LCSC API access requires the `requests` library.
    Install with: pip install kicad-tools[parts]
"""

from .cache import PartsCache, get_default_cache_path
from .importer import (
    ImportOptions,
    ImportResult,
    ImportStage,
    LayoutStyle,
    PartImporter,
)
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
    # Importer
    "PartImporter",
    "ImportResult",
    "ImportOptions",
    "ImportStage",
    "LayoutStyle",
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

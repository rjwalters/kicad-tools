"""Anonymous-LCSC ``InventorySource`` for runs without JLC OpenAPI credentials (Issue #8539).

Wraps :meth:`LCSCClient.lookup_many` (cache bypassed) into the
``ComponentInventory`` shape the submission stock gate consumes. Only parts the
anonymous live API actually answered are returned; cached or offline-catalog
rows are not stock evidence and are left out, so those lines stay unknown.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .jlcpcb_api import ComponentInventory, JLCAPIError
from .lcsc import LCSCClient

LCSC_ANONYMOUS_SOURCE = "lcsc-anonymous"


class LCSCInventorySource:
    """Read stock for exact LCSC codes from LCSC's public (anonymous) API."""

    def __init__(self, client: LCSCClient | None = None) -> None:
        self._client = client

    def get_component_inventory(self, codes: list[str]) -> ComponentInventory:
        client = self._client or LCSCClient(use_cache=False, use_official_api=False)
        try:
            found = client.lookup_many(list(codes), bypass_cache=True)
        except Exception as exc:  # unreachable LCSC must read as an explicit transport error
            partial = getattr(exc, "partial_results", None)
            if not partial:
                raise JLCAPIError(f"LCSC anonymous lookup failed: {type(exc).__name__}") from None
            found = partial
        sources = getattr(found, "sources", {})
        rows = tuple(
            {"componentCode": code.upper(), "stockCount": part.stock}
            for code, part in found.items()
            if sources.get(code) == "anonymous" and type(part.stock) is int
        )
        return ComponentInventory(rows, datetime.now(timezone.utc), LCSC_ANONYMOUS_SOURCE)

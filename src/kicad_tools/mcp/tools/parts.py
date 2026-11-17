"""MCP tools for part search and lookup (Issue #7993).

Thin wrappers over :class:`kicad_tools.parts.LCSCClient`, the same backend as
``kct parts search|lookup``.  The client already tries the live API first and
falls back to the synced offline jlcparts catalog, so these tools only add
input validation and report the coverage the client observed.  An unavailable
backend or an unverified miss is never reported as "not found".
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["parts_lookup", "parts_search"]

MAX_PAGE_SIZE = 100

_LCSC_ID = re.compile(r"^C?\d+$", re.IGNORECASE)


def _price_breaks(part: Any) -> list[dict[str, Any]]:
    return [{"quantity": p.quantity, "unit_price": p.unit_price} for p in part.prices]


def _part_summary(part: Any) -> dict[str, Any]:
    return {
        "lcsc_part": part.lcsc_part,
        "mfr_part": part.mfr_part,
        "manufacturer": part.manufacturer,
        "description": part.description,
        "package": part.package,
        "stock": part.stock,
        "inventory": part.inventory_provenance(),
        "is_basic": part.is_basic,
        "is_preferred": part.is_preferred,
        "best_price": part.best_price,
    }


def _part_detail(part: Any) -> dict[str, Any]:
    detail = _part_summary(part)
    detail.update(
        {
            "category": part.category.value,
            "package_type": part.package_type.value,
            "prices": _price_breaks(part),
            "datasheet_url": part.datasheet_url,
            "product_url": part.product_url,
        }
    )
    return detail


def parts_search(
    query: str,
    package: str | None = None,
    in_stock: bool = False,
    basic_only: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """Search JLCPCB/LCSC parts, falling back to the offline catalog.

    Args:
        query: Free-text query, e.g. ``"10k 0402"``.
        package: Optional package filter, e.g. ``"0402"``.
        in_stock: Only return parts with stock.
        basic_only: Only return JLCPCB basic parts.
        page: 1-based result page.
        page_size: Results per page (1-100).

    Returns:
        ``{"query", "parts", "total_count", "page", "page_size", "source",
        "coverage", "diagnostics"}`` on success.  ``coverage`` is ``live``,
        ``offline`` or ``incomplete``; an empty ``parts`` list is only a
        verified miss when ``coverage`` is ``live``.  ``{"error": ...}`` on bad
        input or when no live or offline source is available.
    """
    if not isinstance(query, str) or not query.strip():
        return {"error": "query must be a non-empty string"}
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        return {"error": "page must be an integer >= 1"}
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= MAX_PAGE_SIZE
    ):
        return {"error": f"page_size must be an integer between 1 and {MAX_PAGE_SIZE}"}

    from kicad_tools.parts import LCSCClient, LCSCUnavailableError

    try:
        result = LCSCClient().search(
            query.strip(),
            page=page,
            page_size=page_size,
            in_stock=bool(in_stock),
            basic_only=bool(basic_only),
            package=package or None,
        )
    except LCSCUnavailableError as exc:
        return {"error": f"parts search unavailable: {exc}", "status": "unavailable"}

    return {
        "query": result.query,
        "parts": [_part_summary(p) for p in result.parts],
        "total_count": result.total_count,
        "page": result.page,
        "page_size": result.page_size,
        "source": result.source,
        "coverage": result.coverage,
        "diagnostics": list(result.diagnostics),
    }


def parts_lookup(lcsc_part: str) -> dict[str, Any]:
    """Look up one part by LCSC number, falling back to the offline catalog.

    Args:
        lcsc_part: LCSC part number, e.g. ``"C25804"`` (the ``C`` prefix is
            optional).

    Returns:
        ``{"status", "source", "diagnostics", "part"}``.  ``status`` is
        ``found`` (``part`` populated) or ``not_found`` (verified live
        no-match, ``part`` is ``None``).  ``offline_miss`` and ``unavailable``
        mean absence is *not* verified and also carry an ``error``.
        ``{"error": ...}`` on a malformed id or an unavailable backend.
    """
    if not isinstance(lcsc_part, str) or not _LCSC_ID.match(lcsc_part.strip()):
        return {"error": "lcsc_part must be an LCSC number such as 'C25804'"}

    from kicad_tools.parts import LCSCClient, LCSCUnavailableError

    try:
        result = LCSCClient().lookup_result(lcsc_part.strip())
    except LCSCUnavailableError as exc:
        return {"error": f"parts lookup unavailable: {exc}", "status": "unavailable"}

    payload: dict[str, Any] = {
        "status": result.status,
        "source": result.source,
        "diagnostics": list(result.diagnostics),
        "part": _part_detail(result.part) if result.part is not None else None,
    }
    if result.part is None and result.status != "not_found":
        payload["error"] = (
            f"{lcsc_part.strip().upper()} could not be verified ({result.status}); "
            "absence from the available sources is not a confirmed no-match"
        )
    return payload

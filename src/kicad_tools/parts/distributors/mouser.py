"""Mouser product-search client (official API, BYO key).

Credential: ``MOUSER_API_KEY`` (environment only). Mouser authenticates with the
key as a query parameter, so this module never includes the request URL in an
exception or log line.
"""

from __future__ import annotations

import os
from typing import Any

from .base import (
    DistributorError,
    DistributorOffer,
    DistributorSkipped,
    now_iso,
    parse_int,
    parse_price,
)

NAME = "mouser"
API_KEY_ENV = "MOUSER_API_KEY"
SEARCH_URL = "https://api.mouser.com/api/v1/search/partnumber"
# Documented default quota at time of writing; verify against Mouser's current
# API terms. Overridable via MOUSER_DAILY_QUOTA.
DEFAULT_DAILY_QUOTA = 1000
REGION = "mouser-account-default"


class MouserClient:
    name = NAME
    daily_quota = DEFAULT_DAILY_QUOTA

    def __init__(self, api_key: str, session: Any, timeout: float = 20.0) -> None:
        self._api_key = api_key
        self._session = session
        self._timeout = timeout

    @classmethod
    def from_env(cls, session: Any) -> MouserClient:
        key = os.environ.get(API_KEY_ENV, "").strip()
        if not key:
            raise DistributorSkipped(f"Mouser skipped: {API_KEY_ENV} is not set")
        client = cls(key, session)
        quota = os.environ.get("MOUSER_DAILY_QUOTA", "").strip()
        if quota.isdigit():
            client.daily_quota = int(quota)
        return client

    def search(self, mpn: str) -> list[DistributorOffer]:
        body = {"SearchByPartRequest": {"mouserPartNumber": mpn, "partSearchOptions": "Exact"}}
        try:
            resp = self._session.post(
                SEARCH_URL, params={"apiKey": self._api_key}, json=body, timeout=self._timeout
            )
        except Exception as exc:  # noqa: BLE001 - message must not leak the URL/key
            raise DistributorError(f"Mouser request failed ({type(exc).__name__})") from None
        if resp.status_code == 429:
            raise DistributorError("Mouser rate limit reached (HTTP 429)")
        if resp.status_code != 200:
            raise DistributorError(f"Mouser request failed (HTTP {resp.status_code})")
        try:
            data = resp.json()
        except ValueError:
            raise DistributorError("Mouser returned a non-JSON response") from None
        errors = data.get("Errors") or []
        if errors:
            raise DistributorError(f"Mouser API error: {errors[0].get('Code', 'unknown')}")
        return parse_mouser(data, mpn)


def parse_mouser(data: dict, query_mpn: str) -> list[DistributorOffer]:
    fetched = now_iso()
    parts = ((data.get("SearchResults") or {}).get("Parts")) or []
    offers = []
    for p in parts:
        mpn = p.get("ManufacturerPartNumber") or ""
        if mpn.upper() != query_mpn.upper():
            continue
        breaks = []
        currency = "USD"
        for pb in p.get("PriceBreaks") or []:
            price = parse_price(pb.get("Price"))
            if price is not None:
                breaks.append((parse_int(pb.get("Quantity")), price))
            currency = pb.get("Currency") or currency
        stock = parse_int(p.get("AvailabilityInStock") or p.get("Availability"))
        packaging = None
        for attr in p.get("ProductAttributes") or []:
            if (attr.get("AttributeName") or "").lower() == "packaging":
                packaging = attr.get("AttributeValue")
        offers.append(
            DistributorOffer(
                mpn=mpn,
                manufacturer=p.get("Manufacturer"),
                distributor=NAME,
                distributor_pn=p.get("MouserPartNumber") or "",
                stock=stock,
                currency=currency,
                region=REGION,
                fetched_at=fetched,
                price_breaks=breaks,
                lead_time=p.get("LeadTime") or None,
                lifecycle=p.get("LifecycleStatus") or None,
                packaging=packaging,
            )
        )
    return offers

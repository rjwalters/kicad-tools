"""DigiKey product-search client (official API v4, OAuth2 client credentials).

Credentials: ``DIGIKEY_CLIENT_ID`` and ``DIGIKEY_CLIENT_SECRET`` (environment
only). The access token is fetched in-process and held in memory; it is never
persisted. Optional locale: ``DIGIKEY_LOCALE_SITE`` (default ``US``),
``DIGIKEY_LOCALE_CURRENCY`` (default ``USD``).
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
)

NAME = "digikey"
ID_ENV = "DIGIKEY_CLIENT_ID"
SECRET_ENV = "DIGIKEY_CLIENT_SECRET"
TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
SEARCH_URL = "https://api.digikey.com/products/v4/search/keyword"
# Documented default quota at time of writing; verify against DigiKey's current
# API terms. Overridable via DIGIKEY_DAILY_QUOTA.
DEFAULT_DAILY_QUOTA = 1000


class DigiKeyClient:
    name = NAME
    daily_quota = DEFAULT_DAILY_QUOTA

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        session: Any,
        site: str = "US",
        currency: str = "USD",
        timeout: float = 20.0,
    ) -> None:
        self._id = client_id
        self._secret = client_secret
        self._session = session
        self._site = site
        self._currency = currency
        self._timeout = timeout
        self._token: str | None = None

    @classmethod
    def from_env(cls, session: Any) -> DigiKeyClient:
        cid = os.environ.get(ID_ENV, "").strip()
        sec = os.environ.get(SECRET_ENV, "").strip()
        missing = [n for n, v in ((ID_ENV, cid), (SECRET_ENV, sec)) if not v]
        if missing:
            raise DistributorSkipped(f"DigiKey skipped: {', '.join(missing)} not set")
        client = cls(
            cid,
            sec,
            session,
            site=os.environ.get("DIGIKEY_LOCALE_SITE", "US").strip() or "US",
            currency=os.environ.get("DIGIKEY_LOCALE_CURRENCY", "USD").strip() or "USD",
        )
        quota = os.environ.get("DIGIKEY_DAILY_QUOTA", "").strip()
        if quota.isdigit():
            client.daily_quota = int(quota)
        return client

    def _fetch_token(self) -> str:
        try:
            resp = self._session.post(
                TOKEN_URL,
                data={
                    "client_id": self._id,
                    "client_secret": self._secret,
                    "grant_type": "client_credentials",
                },
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001
            raise DistributorError(f"DigiKey auth failed ({type(exc).__name__})") from None
        if resp.status_code != 200:
            raise DistributorError(f"DigiKey auth failed (HTTP {resp.status_code})")
        try:
            token = resp.json().get("access_token")
        except ValueError:
            token = None
        if not token:
            raise DistributorError("DigiKey auth response had no access token")
        return token

    def search(self, mpn: str) -> list[DistributorOffer]:
        if self._token is None:
            self._token = self._fetch_token()
        headers = {
            "Authorization": f"Bearer {self._token}",
            "X-DIGIKEY-Client-Id": self._id,
            "X-DIGIKEY-Locale-Site": self._site,
            "X-DIGIKEY-Locale-Currency": self._currency,
            "Content-Type": "application/json",
        }
        try:
            resp = self._session.post(
                SEARCH_URL,
                json={"Keywords": mpn, "Limit": 10},
                headers=headers,
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001
            raise DistributorError(f"DigiKey request failed ({type(exc).__name__})") from None
        if resp.status_code == 429:
            raise DistributorError("DigiKey rate limit reached (HTTP 429)")
        if resp.status_code != 200:
            raise DistributorError(f"DigiKey request failed (HTTP {resp.status_code})")
        try:
            data = resp.json()
        except ValueError:
            raise DistributorError("DigiKey returned a non-JSON response") from None
        return parse_digikey(data, mpn, region=self._site, currency=self._currency)


def parse_digikey(data: dict, query_mpn: str, region: str, currency: str) -> list[DistributorOffer]:
    fetched = now_iso()
    offers = []
    for p in data.get("Products") or []:
        mpn = p.get("ManufacturerProductNumber") or ""
        if mpn.upper() != query_mpn.upper():
            continue
        variations = p.get("ProductVariations") or []
        # Prefer the first variation with a price table (usually cut tape).
        var = next((v for v in variations if v.get("StandardPricing")), None) or (
            variations[0] if variations else {}
        )
        breaks = [
            (parse_int(b.get("BreakQuantity")), float(b["UnitPrice"]))
            for b in var.get("StandardPricing") or []
            if b.get("UnitPrice") is not None
        ]
        lead_weeks = p.get("ManufacturerLeadWeeks")
        offers.append(
            DistributorOffer(
                mpn=mpn,
                manufacturer=(p.get("Manufacturer") or {}).get("Name"),
                distributor=NAME,
                distributor_pn=var.get("DigiKeyProductNumber") or "",
                stock=parse_int(p.get("QuantityAvailable")),
                currency=currency,
                region=region,
                fetched_at=fetched,
                price_breaks=breaks,
                lead_time=f"{lead_weeks} weeks" if lead_weeks else None,
                lifecycle=(p.get("ProductStatus") or {}).get("Status"),
                packaging=(var.get("PackageType") or {}).get("Name"),
            )
        )
    return offers

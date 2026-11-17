"""Orchestrates distributor lookups: credentials, cache, quota, comparison."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import DistributorError, DistributorOffer, DistributorSkipped
from .cache import DistributorCache
from .digikey import DigiKeyClient
from .mouser import MouserClient

DEFAULT_TTL_SECONDS = 3600


@dataclass
class StockReport:
    offers: list[DistributorOffer] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)  # "mpn@distributor"


def sort_offers(offers: list[DistributorOffer]) -> list[DistributorOffer]:
    """Purchasable-now first, then lowest unit price, then name (stable)."""
    return sorted(
        offers,
        key=lambda o: (
            not o.purchasable,
            o.unit_price is None,
            o.unit_price if o.unit_price is not None else 0.0,
            o.mpn,
            o.distributor,
        ),
    )


def _default_session() -> Any:
    try:
        import requests
    except ImportError as exc:  # pragma: no cover
        raise DistributorSkipped(
            "the 'requests' library is required for distributor lookups (install the 'parts' extra)"
        ) from exc
    return requests.Session()


def query_stock(
    mpns: list[str],
    quantity: int,
    *,
    cache: DistributorCache | None = None,
    session: Any = None,
    use_cache: bool = True,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    clients: list[Any] | None = None,
) -> StockReport:
    report = StockReport()
    cache = cache or DistributorCache()
    if clients is None:
        clients = []
        for factory in (MouserClient.from_env, DigiKeyClient.from_env):
            try:
                if session is None:
                    session = _default_session()
                clients.append(factory(session))
            except DistributorSkipped as exc:
                report.notices.append(str(exc))
    for client in clients:
        for mpn in mpns:
            region = getattr(client, "region", None) or _region_of(client)
            cached = cache.get(client.name, mpn, region) if use_cache else None
            if cached and cached[1] <= ttl_seconds:
                offers, age = cached
                _add(report, client.name, mpn, offers, quantity, age)
                continue
            if cache.calls_today(client.name) >= client.daily_quota:
                msg = f"{client.name}: daily quota ({client.daily_quota} calls) exhausted"
                if cached:
                    msg += f"; showing stale cache for {mpn}"
                    _add(report, client.name, mpn, cached[0], quantity, cached[1])
                report.notices.append(msg)
                continue
            cache.record_call(client.name)
            try:
                offers = client.search(mpn)
            except DistributorError as exc:
                report.notices.append(f"{client.name}: {mpn}: {exc}")
                continue
            if offers:
                cache.put(client.name, mpn, offers[0].region, offers)
            _add(report, client.name, mpn, offers, quantity, None)
    report.offers = sort_offers(report.offers)
    return report


def _region_of(client: Any) -> str:
    # DigiKey carries its locale on the client; Mouser has a fixed label.
    from .mouser import REGION

    return getattr(client, "_site", None) or REGION


def _add(
    report: StockReport,
    name: str,
    mpn: str,
    offers: list[DistributorOffer],
    quantity: int,
    age: float | None,
) -> None:
    if not offers:
        report.not_found.append(f"{mpn}@{name}")
        return
    for o in offers:
        o2 = o.at_quantity(quantity)
        if age is not None:
            o2.age_seconds = age
            o2.from_cache = True
        report.offers.append(o2)


__all__ = ["StockReport", "query_stock", "sort_offers"]

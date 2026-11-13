"""Shared types for authorised-distributor stock/price lookups (issue #8892)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class DistributorSkipped(Exception):
    """The source is not usable (missing credential, quota exhausted).

    The message is safe to print: it names environment variables, never values.
    """


class DistributorError(Exception):
    """A request failed. The message never contains URLs, keys or headers."""


@dataclass
class DistributorOffer:
    """One distributor's offer for one manufacturer part number."""

    mpn: str
    distributor: str
    distributor_pn: str
    stock: int
    currency: str
    region: str
    fetched_at: str  # ISO-8601 UTC timestamp of the API read
    price_breaks: list[tuple[int, float]] = field(default_factory=list)
    lead_time: str | None = None
    lifecycle: str | None = None
    packaging: str | None = None
    manufacturer: str | None = None
    # Filled by :meth:`at_quantity`.
    quantity: int | None = None
    unit_price: float | None = None
    price_flag: str | None = None
    # Filled by the service when served from the local cache.
    age_seconds: float | None = None
    from_cache: bool = False

    def at_quantity(self, quantity: int) -> DistributorOffer:
        """Return a copy with ``unit_price`` picked from the price breaks."""
        price, flag = pick_price_break(self.price_breaks, quantity)
        return DistributorOffer(
            **{
                **self.__dict__,
                "quantity": quantity,
                "unit_price": price,
                "price_flag": flag,
            }
        )

    @property
    def purchasable(self) -> bool:
        """True when stock covers the requested quantity (or any, if unset)."""
        need = self.quantity or 1
        return self.stock >= need

    def to_dict(self) -> dict[str, Any]:
        return {
            "mpn": self.mpn,
            "manufacturer": self.manufacturer,
            "distributor": self.distributor,
            "distributor_pn": self.distributor_pn,
            "stock": self.stock,
            "purchasable": self.purchasable,
            "quantity": self.quantity,
            "unit_price": self.unit_price,
            "currency": self.currency,
            "price_flag": self.price_flag,
            "lead_time": self.lead_time,
            "lifecycle": self.lifecycle,
            "packaging": self.packaging,
            "region": self.region,
            "fetched_at": self.fetched_at,
            "age_seconds": self.age_seconds,
            "from_cache": self.from_cache,
        }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pick_price_break(
    breaks: list[tuple[int, float]], quantity: int
) -> tuple[float | None, str | None]:
    """Pick the unit price for *quantity* from ``(break_qty, unit_price)`` pairs.

    Uses the highest break at or below *quantity*. When *quantity* is below the
    lowest break, the lowest break is used and the result is flagged.
    """
    if not breaks:
        return None, "no price breaks"
    ordered = sorted(breaks)
    chosen = None
    for brk_qty, price in ordered:
        if brk_qty <= quantity:
            chosen = (brk_qty, price)
    if chosen is None:
        lowest = ordered[0]
        return lowest[1], f"below minimum price break ({lowest[0]}); using that break"
    return chosen[1], None


def parse_price(text: Any) -> float | None:
    """Parse '$1,234.50', '1,23 €', 0.5 -> float. Returns None if unparseable."""
    if isinstance(text, (int, float)):
        return float(text)
    if not isinstance(text, str):
        return None
    s = "".join(c for c in text if c.isdigit() or c in ".,")
    if not s:
        return None
    if "," in s and "." in s:
        s = (
            s.replace(",", "")
            if s.rfind(".") > s.rfind(",")
            else s.replace(".", "").replace(",", ".")
        )
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head.replace(',', '')}.{tail}" if len(tail) <= 2 else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_int(text: Any) -> int:
    """Parse '1,234 In Stock' / 1234 / '' into an int (0 if no digits)."""
    if isinstance(text, (int, float)):
        return int(text)
    digits = "".join(c for c in str(text or "").split(" ")[0] if c.isdigit())
    return int(digits) if digits else 0

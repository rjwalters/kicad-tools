"""SQLite cache + daily quota counter for distributor offers.

Stores offers (price breaks, not credentials) keyed by distributor/MPN/region so
entries can show their age. Quota counts only live API calls, per UTC day.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .base import DistributorOffer


class DistributorCache:
    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            from ..cache import get_default_cache_path

            db_path = get_default_cache_path().with_name("distributors.db")
        self.db_path = Path(db_path)
        with closing(self._conn()) as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS offers (distributor TEXT, mpn TEXT, region TEXT,"
                " fetched_at TEXT, payload TEXT, PRIMARY KEY (distributor, mpn, region))"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS quota (distributor TEXT, day TEXT, calls INTEGER,"
                " PRIMARY KEY (distributor, day))"
            )
            c.commit()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def get(
        self, distributor: str, mpn: str, region: str
    ) -> tuple[list[DistributorOffer], float] | None:
        """Return ``(offers, age_seconds)`` or None."""
        with closing(self._conn()) as c:
            row = c.execute(
                "SELECT fetched_at, payload FROM offers WHERE distributor=? AND mpn=? AND region=?",
                (distributor, mpn.upper(), region),
            ).fetchone()
        if not row:
            return None
        fetched_at, payload = row
        offers = []
        for d in json.loads(payload):
            d["price_breaks"] = [tuple(b) for b in d["price_breaks"]]
            offers.append(DistributorOffer(**d))
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)).total_seconds()
        return offers, max(age, 0.0)

    def put(self, distributor: str, mpn: str, region: str, offers: list[DistributorOffer]) -> None:
        payload = json.dumps(
            [
                {
                    k: v
                    for k, v in o.__dict__.items()
                    if k
                    not in ("quantity", "unit_price", "price_flag", "age_seconds", "from_cache")
                }
                for o in offers
            ]
        )
        stamp = offers[0].fetched_at if offers else datetime.now(timezone.utc).isoformat()
        with closing(self._conn()) as c:
            c.execute(
                "INSERT OR REPLACE INTO offers VALUES (?,?,?,?,?)",
                (distributor, mpn.upper(), region, stamp, payload),
            )
            c.commit()

    def calls_today(self, distributor: str) -> int:
        with closing(self._conn()) as c:
            row = c.execute(
                "SELECT calls FROM quota WHERE distributor=? AND day=?",
                (distributor, self._today()),
            ).fetchone()
        return row[0] if row else 0

    def record_call(self, distributor: str) -> None:
        with closing(self._conn()) as c:
            c.execute(
                "INSERT INTO quota VALUES (?,?,1) ON CONFLICT(distributor, day)"
                " DO UPDATE SET calls = calls + 1",
                (distributor, self._today()),
            )
            c.commit()

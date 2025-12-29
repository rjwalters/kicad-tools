"""
SQLite cache for parts database.

Caches LCSC part lookups to reduce API calls and enable offline use.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, List, Optional

from .models import Part, PartCategory, PackageType, PartPrice


def get_default_cache_path() -> Path:
    """Get default cache file path."""
    # Use XDG cache directory if available, otherwise ~/.cache
    import os

    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        cache_dir = Path(xdg_cache) / "kicad-tools"
    else:
        cache_dir = Path.home() / ".cache" / "kicad-tools"

    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "parts.db"


class PartsCache:
    """
    SQLite-backed cache for LCSC parts.

    Stores parts data locally to reduce API requests and enable
    offline lookups. Cache entries expire after a configurable TTL.

    Example::

        cache = PartsCache()

        # Check cache first
        part = cache.get("C123456")
        if part is None:
            part = lcsc_client.lookup("C123456")
            cache.put(part)

        # Bulk operations
        cache.put_many(parts_list)
        cached = cache.get_many(["C123", "C456"])

        # Cache stats
        stats = cache.stats()
        print(f"Cached parts: {stats['total']}")
    """

    SCHEMA_VERSION = 1
    DEFAULT_TTL_DAYS = 7

    def __init__(
        self,
        db_path: Optional[Path] = None,
        ttl_days: int = DEFAULT_TTL_DAYS,
    ):
        """
        Initialize the cache.

        Args:
            db_path: Path to SQLite database file (default: ~/.cache/kicad-tools/parts.db)
            ttl_days: Number of days before cache entries expire
        """
        self.db_path = db_path or get_default_cache_path()
        self.ttl = timedelta(days=ttl_days)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema."""
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS parts (
                    lcsc_part TEXT PRIMARY KEY,
                    mfr_part TEXT,
                    manufacturer TEXT,
                    description TEXT,
                    category TEXT,
                    package TEXT,
                    package_type TEXT,
                    value TEXT,
                    tolerance TEXT,
                    voltage_rating TEXT,
                    power_rating TEXT,
                    temperature_range TEXT,
                    specs TEXT,
                    stock INTEGER,
                    min_order INTEGER,
                    prices TEXT,
                    is_basic INTEGER,
                    is_preferred INTEGER,
                    datasheet_url TEXT,
                    product_url TEXT,
                    fetched_at TEXT,
                    cached_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_parts_mfr ON parts(mfr_part);
                CREATE INDEX IF NOT EXISTS idx_parts_category ON parts(category);
                CREATE INDEX IF NOT EXISTS idx_parts_cached ON parts(cached_at);
            """)

            # Check schema version
            cursor = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
            row = cursor.fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(self.SCHEMA_VERSION),),
                )
            elif int(row[0]) < self.SCHEMA_VERSION:
                self._migrate(conn, int(row[0]))

    def _migrate(self, conn: sqlite3.Connection, from_version: int) -> None:
        """Migrate database schema."""
        # Future migrations would go here
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(self.SCHEMA_VERSION),),
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Get database connection."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _part_to_row(self, part: Part) -> dict:
        """Convert Part to database row."""
        return {
            "lcsc_part": part.lcsc_part,
            "mfr_part": part.mfr_part,
            "manufacturer": part.manufacturer,
            "description": part.description,
            "category": part.category.value,
            "package": part.package,
            "package_type": part.package_type.value,
            "value": part.value,
            "tolerance": part.tolerance,
            "voltage_rating": part.voltage_rating,
            "power_rating": part.power_rating,
            "temperature_range": part.temperature_range,
            "specs": json.dumps(part.specs),
            "stock": part.stock,
            "min_order": part.min_order,
            "prices": json.dumps(
                [{"quantity": p.quantity, "unit_price": p.unit_price, "currency": p.currency} for p in part.prices]
            ),
            "is_basic": 1 if part.is_basic else 0,
            "is_preferred": 1 if part.is_preferred else 0,
            "datasheet_url": part.datasheet_url,
            "product_url": part.product_url,
            "fetched_at": part.fetched_at.isoformat() if part.fetched_at else None,
            "cached_at": datetime.now().isoformat(),
        }

    def _row_to_part(self, row: sqlite3.Row) -> Part:
        """Convert database row to Part."""
        prices_data = json.loads(row["prices"]) if row["prices"] else []
        prices = [
            PartPrice(
                quantity=p["quantity"],
                unit_price=p["unit_price"],
                currency=p.get("currency", "USD"),
            )
            for p in prices_data
        ]

        specs = json.loads(row["specs"]) if row["specs"] else {}

        fetched_at = None
        if row["fetched_at"]:
            fetched_at = datetime.fromisoformat(row["fetched_at"])

        return Part(
            lcsc_part=row["lcsc_part"],
            mfr_part=row["mfr_part"] or "",
            manufacturer=row["manufacturer"] or "",
            description=row["description"] or "",
            category=PartCategory(row["category"]) if row["category"] else PartCategory.OTHER,
            package=row["package"] or "",
            package_type=PackageType(row["package_type"]) if row["package_type"] else PackageType.UNKNOWN,
            value=row["value"] or "",
            tolerance=row["tolerance"] or "",
            voltage_rating=row["voltage_rating"] or "",
            power_rating=row["power_rating"] or "",
            temperature_range=row["temperature_range"] or "",
            specs=specs,
            stock=row["stock"] or 0,
            min_order=row["min_order"] or 1,
            prices=prices,
            is_basic=bool(row["is_basic"]),
            is_preferred=bool(row["is_preferred"]),
            datasheet_url=row["datasheet_url"] or "",
            product_url=row["product_url"] or "",
            fetched_at=fetched_at,
        )

    def _is_expired(self, cached_at: str) -> bool:
        """Check if cache entry is expired."""
        cached_time = datetime.fromisoformat(cached_at)
        return datetime.now() - cached_time > self.ttl

    def get(self, lcsc_part: str, ignore_expiry: bool = False) -> Optional[Part]:
        """
        Get a part from cache.

        Args:
            lcsc_part: LCSC part number (e.g., "C123456")
            ignore_expiry: If True, return expired entries

        Returns:
            Part if found and not expired, None otherwise
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM parts WHERE lcsc_part = ?",
                (lcsc_part.upper(),),
            )
            row = cursor.fetchone()

            if row is None:
                return None

            if not ignore_expiry and self._is_expired(row["cached_at"]):
                return None

            return self._row_to_part(row)

    def get_many(self, lcsc_parts: List[str], ignore_expiry: bool = False) -> dict[str, Part]:
        """
        Get multiple parts from cache.

        Args:
            lcsc_parts: List of LCSC part numbers
            ignore_expiry: If True, return expired entries

        Returns:
            Dict mapping part numbers to Parts (missing entries not included)
        """
        if not lcsc_parts:
            return {}

        result = {}
        with self._connect() as conn:
            placeholders = ",".join("?" * len(lcsc_parts))
            cursor = conn.execute(
                f"SELECT * FROM parts WHERE lcsc_part IN ({placeholders})",
                [p.upper() for p in lcsc_parts],
            )

            for row in cursor:
                if not ignore_expiry and self._is_expired(row["cached_at"]):
                    continue
                part = self._row_to_part(row)
                result[part.lcsc_part] = part

        return result

    def put(self, part: Part) -> None:
        """
        Store a part in cache.

        Args:
            part: Part to cache
        """
        row = self._part_to_row(part)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO parts (
                    lcsc_part, mfr_part, manufacturer, description,
                    category, package, package_type, value,
                    tolerance, voltage_rating, power_rating, temperature_range,
                    specs, stock, min_order, prices,
                    is_basic, is_preferred, datasheet_url, product_url,
                    fetched_at, cached_at
                ) VALUES (
                    :lcsc_part, :mfr_part, :manufacturer, :description,
                    :category, :package, :package_type, :value,
                    :tolerance, :voltage_rating, :power_rating, :temperature_range,
                    :specs, :stock, :min_order, :prices,
                    :is_basic, :is_preferred, :datasheet_url, :product_url,
                    :fetched_at, :cached_at
                )
                """,
                row,
            )

    def put_many(self, parts: List[Part]) -> None:
        """
        Store multiple parts in cache.

        Args:
            parts: List of parts to cache
        """
        if not parts:
            return

        rows = [self._part_to_row(p) for p in parts]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO parts (
                    lcsc_part, mfr_part, manufacturer, description,
                    category, package, package_type, value,
                    tolerance, voltage_rating, power_rating, temperature_range,
                    specs, stock, min_order, prices,
                    is_basic, is_preferred, datasheet_url, product_url,
                    fetched_at, cached_at
                ) VALUES (
                    :lcsc_part, :mfr_part, :manufacturer, :description,
                    :category, :package, :package_type, :value,
                    :tolerance, :voltage_rating, :power_rating, :temperature_range,
                    :specs, :stock, :min_order, :prices,
                    :is_basic, :is_preferred, :datasheet_url, :product_url,
                    :fetched_at, :cached_at
                )
                """,
                rows,
            )

    def delete(self, lcsc_part: str) -> bool:
        """
        Delete a part from cache.

        Args:
            lcsc_part: LCSC part number

        Returns:
            True if part was deleted, False if not found
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM parts WHERE lcsc_part = ?",
                (lcsc_part.upper(),),
            )
            return cursor.rowcount > 0

    def clear(self) -> int:
        """
        Clear all cached parts.

        Returns:
            Number of parts deleted
        """
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM parts")
            return cursor.rowcount

    def clear_expired(self) -> int:
        """
        Remove expired entries from cache.

        Returns:
            Number of entries removed
        """
        cutoff = (datetime.now() - self.ttl).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM parts WHERE cached_at < ?",
                (cutoff,),
            )
            return cursor.rowcount

    def stats(self) -> dict:
        """
        Get cache statistics.

        Returns:
            Dict with cache stats
        """
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM parts").fetchone()[0]

            cutoff = (datetime.now() - self.ttl).isoformat()
            valid = conn.execute(
                "SELECT COUNT(*) FROM parts WHERE cached_at >= ?",
                (cutoff,),
            ).fetchone()[0]

            expired = total - valid

            # Get oldest and newest
            oldest = conn.execute(
                "SELECT MIN(cached_at) FROM parts"
            ).fetchone()[0]
            newest = conn.execute(
                "SELECT MAX(cached_at) FROM parts"
            ).fetchone()[0]

            # Category breakdown
            categories = {}
            cursor = conn.execute(
                "SELECT category, COUNT(*) FROM parts GROUP BY category"
            )
            for row in cursor:
                categories[row[0]] = row[1]

        return {
            "total": total,
            "valid": valid,
            "expired": expired,
            "oldest": oldest,
            "newest": newest,
            "categories": categories,
            "db_path": str(self.db_path),
            "ttl_days": self.ttl.days,
        }

    def contains(self, lcsc_part: str, ignore_expiry: bool = False) -> bool:
        """
        Check if part is in cache.

        Args:
            lcsc_part: LCSC part number
            ignore_expiry: If True, include expired entries

        Returns:
            True if part is cached (and not expired unless ignore_expiry)
        """
        with self._connect() as conn:
            if ignore_expiry:
                cursor = conn.execute(
                    "SELECT 1 FROM parts WHERE lcsc_part = ?",
                    (lcsc_part.upper(),),
                )
            else:
                cutoff = (datetime.now() - self.ttl).isoformat()
                cursor = conn.execute(
                    "SELECT 1 FROM parts WHERE lcsc_part = ? AND cached_at >= ?",
                    (lcsc_part.upper(), cutoff),
                )
            return cursor.fetchone() is not None

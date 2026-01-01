"""
SQLite cache for downloaded datasheets.

Tracks downloaded datasheets and their metadata, enabling offline access
and reducing redundant downloads.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .models import Datasheet

logger = logging.getLogger(__name__)


def get_default_cache_path() -> Path:
    """Get default cache directory path."""
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        cache_dir = Path(xdg_cache) / "kicad-tools" / "datasheets"
    else:
        cache_dir = Path.home() / ".cache" / "kicad-tools" / "datasheets"

    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


class DatasheetCache:
    """
    SQLite-backed cache for downloaded datasheets.

    Stores datasheet files and metadata locally to reduce downloads
    and enable offline access.

    Example::

        cache = DatasheetCache()

        # Check if datasheet is cached
        if cache.is_cached("STM32F103C8T6"):
            datasheet = cache.get("STM32F103C8T6")
            print(f"Found at: {datasheet.local_path}")

        # List all cached datasheets
        for ds in cache.list():
            print(f"{ds.part_number}: {ds.file_size_mb:.1f} MB")

        # Cache stats
        stats = cache.stats()
        print(f"Total cached: {stats['total_count']}")
    """

    SCHEMA_VERSION = 1
    DEFAULT_TTL_DAYS = 90  # Datasheets don't change often

    def __init__(
        self,
        cache_dir: Path | None = None,
        ttl_days: int = DEFAULT_TTL_DAYS,
    ):
        """
        Initialize the cache.

        Args:
            cache_dir: Directory for cached files (default: ~/.cache/kicad-tools/datasheets)
            ttl_days: Number of days before cache entries expire
        """
        self.cache_dir = cache_dir or get_default_cache_path()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.cache_dir / "index.db"
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

                CREATE TABLE IF NOT EXISTS datasheets (
                    part_number TEXT PRIMARY KEY,
                    manufacturer TEXT,
                    local_path TEXT,
                    source_url TEXT,
                    source TEXT,
                    downloaded_at TEXT,
                    file_size INTEGER,
                    page_count INTEGER,
                    cached_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_datasheets_mfr
                    ON datasheets(manufacturer);
                CREATE INDEX IF NOT EXISTS idx_datasheets_cached
                    ON datasheets(cached_at);
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

    def _datasheet_to_row(self, ds: Datasheet) -> dict:
        """Convert Datasheet to database row."""
        return {
            "part_number": ds.part_number,
            "manufacturer": ds.manufacturer,
            "local_path": str(ds.local_path),
            "source_url": ds.source_url,
            "source": ds.source,
            "downloaded_at": ds.downloaded_at.isoformat(),
            "file_size": ds.file_size,
            "page_count": ds.page_count,
            "cached_at": datetime.now().isoformat(),
        }

    def _row_to_datasheet(self, row: sqlite3.Row) -> Datasheet:
        """Convert database row to Datasheet."""
        return Datasheet(
            part_number=row["part_number"],
            manufacturer=row["manufacturer"] or "",
            local_path=Path(row["local_path"]),
            source_url=row["source_url"] or "",
            source=row["source"] or "",
            downloaded_at=datetime.fromisoformat(row["downloaded_at"]),
            file_size=row["file_size"] or 0,
            page_count=row["page_count"],
        )

    def _is_expired(self, cached_at: str) -> bool:
        """Check if cache entry is expired."""
        cached_time = datetime.fromisoformat(cached_at)
        return datetime.now() - cached_time > self.ttl

    def get_datasheet_path(self, part_number: str) -> Path:
        """Get the expected path for a datasheet file."""
        # Normalize part number for filesystem
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in part_number)
        return self.cache_dir / safe_name / "datasheet.pdf"

    def is_cached(self, part_number: str, ignore_expiry: bool = False) -> bool:
        """
        Check if a datasheet is cached.

        Args:
            part_number: Part number to check
            ignore_expiry: If True, include expired entries

        Returns:
            True if datasheet is cached (and not expired unless ignore_expiry)
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT cached_at, local_path FROM datasheets WHERE part_number = ?",
                (part_number,),
            )
            row = cursor.fetchone()

            if row is None:
                return False

            # Check if file still exists
            if not Path(row["local_path"]).exists():
                return False

            if not ignore_expiry and self._is_expired(row["cached_at"]):
                return False

            return True

    def get(self, part_number: str, ignore_expiry: bool = False) -> Datasheet | None:
        """
        Get a cached datasheet.

        Args:
            part_number: Part number to look up
            ignore_expiry: If True, return expired entries

        Returns:
            Datasheet if found and not expired, None otherwise
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM datasheets WHERE part_number = ?",
                (part_number,),
            )
            row = cursor.fetchone()

            if row is None:
                return None

            # Check if file still exists
            if not Path(row["local_path"]).exists():
                # Clean up orphaned entry
                conn.execute(
                    "DELETE FROM datasheets WHERE part_number = ?",
                    (part_number,),
                )
                return None

            if not ignore_expiry and self._is_expired(row["cached_at"]):
                return None

            return self._row_to_datasheet(row)

    def put(self, datasheet: Datasheet) -> None:
        """
        Store a datasheet in cache.

        Args:
            datasheet: Datasheet to cache
        """
        row = self._datasheet_to_row(datasheet)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO datasheets (
                    part_number, manufacturer, local_path, source_url,
                    source, downloaded_at, file_size, page_count, cached_at
                ) VALUES (
                    :part_number, :manufacturer, :local_path, :source_url,
                    :source, :downloaded_at, :file_size, :page_count, :cached_at
                )
                """,
                row,
            )

    def delete(self, part_number: str) -> bool:
        """
        Delete a cached datasheet.

        Args:
            part_number: Part number to delete

        Returns:
            True if deleted, False if not found
        """
        with self._connect() as conn:
            # Get path first
            cursor = conn.execute(
                "SELECT local_path FROM datasheets WHERE part_number = ?",
                (part_number,),
            )
            row = cursor.fetchone()

            if row is None:
                return False

            # Delete file
            local_path = Path(row["local_path"])
            if local_path.exists():
                local_path.unlink()
                # Remove parent directory if empty
                if local_path.parent.exists() and not any(local_path.parent.iterdir()):
                    local_path.parent.rmdir()

            # Delete database entry
            conn.execute(
                "DELETE FROM datasheets WHERE part_number = ?",
                (part_number,),
            )
            return True

    def list(self, ignore_expiry: bool = False) -> list[Datasheet]:
        """
        List all cached datasheets.

        Args:
            ignore_expiry: If True, include expired entries

        Returns:
            List of cached Datasheet objects
        """
        results = []
        with self._connect() as conn:
            cursor = conn.execute("SELECT * FROM datasheets ORDER BY part_number")
            for row in cursor:
                # Check expiry
                if not ignore_expiry and self._is_expired(row["cached_at"]):
                    continue

                # Check file exists
                if not Path(row["local_path"]).exists():
                    continue

                results.append(self._row_to_datasheet(row))

        return results

    def clear(self) -> int:
        """
        Clear all cached datasheets.

        Returns:
            Number of entries removed
        """
        with self._connect() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM datasheets")
            count = cursor.fetchone()[0]

            # Delete all files
            for item in self.cache_dir.iterdir():
                if item.is_dir() and item.name != "index.db":
                    shutil.rmtree(item)

            # Clear database
            conn.execute("DELETE FROM datasheets")
            return count

    def clear_expired(self) -> int:
        """
        Remove expired entries from cache.

        Returns:
            Number of entries removed
        """
        cutoff = (datetime.now() - self.ttl).isoformat()
        removed = 0

        with self._connect() as conn:
            # Get expired entries
            cursor = conn.execute(
                "SELECT part_number, local_path FROM datasheets WHERE cached_at < ?",
                (cutoff,),
            )
            for row in cursor:
                # Delete file
                local_path = Path(row["local_path"])
                if local_path.exists():
                    local_path.unlink()
                    if local_path.parent.exists() and not any(local_path.parent.iterdir()):
                        local_path.parent.rmdir()
                removed += 1

            # Delete database entries
            conn.execute("DELETE FROM datasheets WHERE cached_at < ?", (cutoff,))

        return removed

    def clear_older_than(self, days: int) -> int:
        """
        Remove entries older than specified days.

        Args:
            days: Remove entries older than this many days

        Returns:
            Number of entries removed
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        removed = 0

        with self._connect() as conn:
            # Get old entries
            cursor = conn.execute(
                "SELECT part_number, local_path FROM datasheets WHERE cached_at < ?",
                (cutoff,),
            )
            for row in cursor:
                # Delete file
                local_path = Path(row["local_path"])
                if local_path.exists():
                    local_path.unlink()
                    if local_path.parent.exists() and not any(local_path.parent.iterdir()):
                        local_path.parent.rmdir()
                removed += 1

            # Delete database entries
            conn.execute("DELETE FROM datasheets WHERE cached_at < ?", (cutoff,))

        return removed

    def stats(self) -> dict:
        """
        Get cache statistics.

        Returns:
            Dict with cache stats
        """
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM datasheets").fetchone()[0]

            cutoff = (datetime.now() - self.ttl).isoformat()
            valid = conn.execute(
                "SELECT COUNT(*) FROM datasheets WHERE cached_at >= ?",
                (cutoff,),
            ).fetchone()[0]

            total_size = conn.execute(
                "SELECT COALESCE(SUM(file_size), 0) FROM datasheets"
            ).fetchone()[0]

            oldest = conn.execute("SELECT MIN(cached_at) FROM datasheets").fetchone()[0]
            newest = conn.execute("SELECT MAX(cached_at) FROM datasheets").fetchone()[0]

            # Source breakdown
            sources = {}
            cursor = conn.execute("SELECT source, COUNT(*) FROM datasheets GROUP BY source")
            for row in cursor:
                sources[row[0] or "unknown"] = row[1]

        return {
            "total_count": total,
            "valid_count": valid,
            "expired_count": total - valid,
            "total_size_bytes": total_size,
            "total_size_mb": total_size / (1024 * 1024),
            "oldest": oldest,
            "newest": newest,
            "sources": sources,
            "cache_dir": str(self.cache_dir),
            "ttl_days": self.ttl.days,
        }

"""
SQLite cache for routing results.

Caches expensive routing computations to enable faster iteration during
PCB design. When a designer makes small changes to a PCB, unchanged nets
can reuse their cached routes instead of being re-routed from scratch.

Key features:
- Content-addressable caching using SHA-256 hashes
- Full routing result caching for exact PCB configurations
- Per-net partial route caching for incremental routing
- Automatic cache invalidation based on kicad-tools version
- Configurable TTL and max size limits
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .primitives import Route
    from .rules import DesignRules

logger = logging.getLogger(__name__)

# Version tag for cache invalidation when algorithms change
CACHE_VERSION = "1.0.0"


def get_default_cache_path() -> Path:
    """Get default cache directory path."""
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        cache_dir = Path(xdg_cache) / "kicad-tools" / "routing"
    else:
        cache_dir = Path.home() / ".cache" / "kicad-tools" / "routing"

    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _get_kicad_tools_version() -> str:
    """Get current kicad-tools version for cache invalidation."""
    try:
        from kicad_tools import __version__

        return __version__
    except ImportError:
        return "unknown"


@dataclass
class CacheKey:
    """Content-addressable cache key for routing results.

    The key is computed from a hash of all inputs that affect routing:
    - PCB content (footprints, pads, nets)
    - Design rules (clearances, via sizes, etc.)
    - Grid resolution
    - kicad-tools version (to invalidate on algorithm changes)
    """

    pcb_hash: str
    rules_hash: str
    version: str

    @classmethod
    def compute(
        cls,
        pcb_content: str | bytes,
        rules: DesignRules,
        grid_resolution: float,
    ) -> CacheKey:
        """Compute cache key from routing inputs.

        Args:
            pcb_content: PCB file content or extracted routing-relevant data
            rules: Design rules for routing
            grid_resolution: Routing grid resolution in mm

        Returns:
            CacheKey for this configuration
        """
        # Hash PCB content
        if isinstance(pcb_content, str):
            pcb_content = pcb_content.encode("utf-8")
        pcb_hash = hashlib.sha256(pcb_content).hexdigest()

        # Hash design rules (serialize relevant fields)
        rules_data = {
            "trace_width": rules.trace_width,
            "trace_clearance": rules.trace_clearance,
            "via_drill": rules.via_drill,
            "via_diameter": rules.via_diameter,
            "via_clearance": rules.via_clearance,
            "grid_resolution": grid_resolution,
            "preferred_layer": rules.preferred_layer.value,
            "alternate_layer": rules.alternate_layer.value,
        }
        rules_json = json.dumps(rules_data, sort_keys=True)
        rules_hash = hashlib.sha256(rules_json.encode()).hexdigest()

        return cls(
            pcb_hash=pcb_hash,
            rules_hash=rules_hash,
            version=_get_kicad_tools_version(),
        )

    @property
    def full_key(self) -> str:
        """Combined key string for database lookups."""
        return f"{self.pcb_hash[:16]}:{self.rules_hash[:16]}:{self.version}"


@dataclass
class CachedRoutingResult:
    """Cached result of a routing operation.

    Stores serialized routes along with metadata about the routing run.
    """

    cache_key: str
    routes_data: bytes  # Compressed JSON of routes
    success_count: int
    failure_count: int
    total_segments: int
    total_vias: int
    compute_time_ms: int
    created_at: datetime
    last_accessed: datetime

    @property
    def success_rate(self) -> float:
        """Fraction of nets successfully routed."""
        total = self.success_count + self.failure_count
        if total == 0:
            return 1.0
        return self.success_count / total


@dataclass
class CachedNetRoute:
    """Cached route for a single net.

    Used for incremental routing - when only some nets change,
    unchanged nets can reuse their cached routes.
    """

    pcb_hash: str
    net_id: int
    net_name: str
    route_data: bytes  # Compressed JSON of single route
    pad_positions_hash: str  # Hash of pad positions for this net
    created_at: datetime


class RoutingCache:
    """
    SQLite-backed cache for routing results.

    Stores routing results locally to reduce computation time for
    iterative PCB design workflows. Supports both full routing result
    caching and per-net partial route caching for incremental updates.

    Example::

        cache = RoutingCache()

        # Check for cached result
        key = CacheKey.compute(pcb_content, rules, grid_resolution)
        cached = cache.get(key)
        if cached:
            routes = cache.deserialize_routes(cached.routes_data)
            return routes

        # Route normally and cache result
        routes = autorouter.route_all(nets)
        cache.put(key, routes, statistics)

        # Incremental routing
        unchanged_nets = cache.get_unchanged_net_routes(pcb_hash, net_pad_hashes)
    """

    SCHEMA_VERSION = 1
    DEFAULT_TTL_DAYS = 30
    DEFAULT_MAX_SIZE_MB = 500

    def __init__(
        self,
        cache_dir: Path | None = None,
        ttl_days: int = DEFAULT_TTL_DAYS,
        max_size_mb: int = DEFAULT_MAX_SIZE_MB,
    ):
        """
        Initialize the routing cache.

        Args:
            cache_dir: Directory for cache database (default: ~/.cache/kicad-tools/routing)
            ttl_days: Number of days before cache entries expire
            max_size_mb: Maximum cache size in megabytes (oldest entries evicted)
        """
        self.cache_dir = cache_dir or get_default_cache_path()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.cache_dir / "routing.db"
        self.ttl = timedelta(days=ttl_days)
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema."""
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS routing_results (
                    cache_key TEXT PRIMARY KEY,
                    pcb_hash TEXT NOT NULL,
                    rules_hash TEXT NOT NULL,
                    version TEXT NOT NULL,
                    routes_data BLOB NOT NULL,
                    success_count INTEGER NOT NULL,
                    failure_count INTEGER NOT NULL,
                    total_segments INTEGER NOT NULL,
                    total_vias INTEGER NOT NULL,
                    compute_time_ms INTEGER NOT NULL,
                    data_size INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    last_accessed TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS partial_routes (
                    pcb_hash TEXT NOT NULL,
                    net_id INTEGER NOT NULL,
                    net_name TEXT NOT NULL,
                    route_data BLOB NOT NULL,
                    pad_positions_hash TEXT NOT NULL,
                    data_size INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (pcb_hash, net_id)
                );

                CREATE INDEX IF NOT EXISTS idx_routing_results_pcb
                    ON routing_results(pcb_hash);
                CREATE INDEX IF NOT EXISTS idx_routing_results_accessed
                    ON routing_results(last_accessed);
                CREATE INDEX IF NOT EXISTS idx_partial_routes_hash
                    ON partial_routes(pad_positions_hash);
            """)

            # Check schema version
            cursor = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
            row = cursor.fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(self.SCHEMA_VERSION),),
                )
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('cache_version', ?)",
                    (CACHE_VERSION,),
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

    def _is_expired(self, created_at: str) -> bool:
        """Check if cache entry is expired."""
        created_time = datetime.fromisoformat(created_at)
        return datetime.now() - created_time > self.ttl

    def serialize_routes(self, routes: list[Route]) -> bytes:
        """Serialize routes to compressed bytes for storage.

        Args:
            routes: List of Route objects

        Returns:
            Compressed JSON bytes
        """

        routes_data = []
        for route in routes:
            route_dict = {
                "net": route.net,
                "net_name": route.net_name,
                "segments": [
                    {
                        "x1": seg.x1,
                        "y1": seg.y1,
                        "x2": seg.x2,
                        "y2": seg.y2,
                        "width": seg.width,
                        "layer": seg.layer.value,
                        "net": seg.net,
                        "net_name": seg.net_name,
                    }
                    for seg in route.segments
                ],
                "vias": [
                    {
                        "x": via.x,
                        "y": via.y,
                        "drill": via.drill,
                        "diameter": via.diameter,
                        "layers": [via.layers[0].value, via.layers[1].value],
                        "net": via.net,
                        "net_name": via.net_name,
                    }
                    for via in route.vias
                ],
            }
            routes_data.append(route_dict)

        json_bytes = json.dumps(routes_data).encode("utf-8")
        return zlib.compress(json_bytes)

    def deserialize_routes(self, data: bytes) -> list[Route]:
        """Deserialize routes from compressed bytes.

        Args:
            data: Compressed JSON bytes

        Returns:
            List of Route objects
        """
        from .layers import Layer
        from .primitives import Route, Segment, Via

        json_bytes = zlib.decompress(data)
        routes_data = json.loads(json_bytes.decode("utf-8"))

        routes = []
        for route_dict in routes_data:
            segments = [
                Segment(
                    x1=seg["x1"],
                    y1=seg["y1"],
                    x2=seg["x2"],
                    y2=seg["y2"],
                    width=seg["width"],
                    layer=Layer(seg["layer"]),
                    net=seg["net"],
                    net_name=seg["net_name"],
                )
                for seg in route_dict["segments"]
            ]
            vias = [
                Via(
                    x=via["x"],
                    y=via["y"],
                    drill=via["drill"],
                    diameter=via["diameter"],
                    layers=(Layer(via["layers"][0]), Layer(via["layers"][1])),
                    net=via["net"],
                    net_name=via["net_name"],
                )
                for via in route_dict["vias"]
            ]
            route = Route(
                net=route_dict["net"],
                net_name=route_dict["net_name"],
                segments=segments,
                vias=vias,
            )
            routes.append(route)

        return routes

    def get(self, key: CacheKey, ignore_expiry: bool = False) -> CachedRoutingResult | None:
        """
        Get cached routing result.

        Args:
            key: Cache key for the routing configuration
            ignore_expiry: If True, return expired entries

        Returns:
            CachedRoutingResult if found and valid, None otherwise
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM routing_results
                WHERE cache_key = ? AND version = ?
                """,
                (key.full_key, key.version),
            )
            row = cursor.fetchone()

            if row is None:
                logger.debug(f"Cache miss for key {key.full_key}")
                return None

            if not ignore_expiry and self._is_expired(row["created_at"]):
                logger.debug(f"Cache entry expired for key {key.full_key}")
                return None

            # Update last accessed time
            conn.execute(
                "UPDATE routing_results SET last_accessed = ? WHERE cache_key = ?",
                (datetime.now().isoformat(), key.full_key),
            )

            logger.info(f"Cache hit for key {key.full_key}")
            return CachedRoutingResult(
                cache_key=row["cache_key"],
                routes_data=row["routes_data"],
                success_count=row["success_count"],
                failure_count=row["failure_count"],
                total_segments=row["total_segments"],
                total_vias=row["total_vias"],
                compute_time_ms=row["compute_time_ms"],
                created_at=datetime.fromisoformat(row["created_at"]),
                last_accessed=datetime.fromisoformat(row["last_accessed"]),
            )

    def put(
        self,
        key: CacheKey,
        routes: list[Route],
        statistics: dict,
        compute_time_ms: int = 0,
    ) -> None:
        """
        Store routing result in cache.

        Args:
            key: Cache key for the routing configuration
            routes: List of Route objects
            statistics: Routing statistics dict
            compute_time_ms: Time taken for routing in milliseconds
        """
        routes_data = self.serialize_routes(routes)
        data_size = len(routes_data)
        now = datetime.now().isoformat()

        # Ensure we have space
        self._enforce_size_limit(data_size)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO routing_results (
                    cache_key, pcb_hash, rules_hash, version,
                    routes_data, success_count, failure_count,
                    total_segments, total_vias, compute_time_ms,
                    data_size, created_at, last_accessed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key.full_key,
                    key.pcb_hash,
                    key.rules_hash,
                    key.version,
                    routes_data,
                    statistics.get("routes", len(routes)),
                    statistics.get("failures", 0),
                    statistics.get("segments", sum(len(r.segments) for r in routes)),
                    statistics.get("vias", sum(len(r.vias) for r in routes)),
                    compute_time_ms,
                    data_size,
                    now,
                    now,
                ),
            )

        logger.info(f"Cached routing result for key {key.full_key} ({data_size} bytes)")

    def put_net_route(
        self,
        pcb_hash: str,
        net_id: int,
        net_name: str,
        route: Route,
        pad_positions_hash: str,
    ) -> None:
        """
        Store a single net's route for incremental caching.

        Args:
            pcb_hash: Hash of the PCB (for grouping)
            net_id: Net number
            net_name: Net name
            route: Route object for this net
            pad_positions_hash: Hash of pad positions for this net
        """
        route_data = self.serialize_routes([route])
        data_size = len(route_data)
        now = datetime.now().isoformat()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO partial_routes (
                    pcb_hash, net_id, net_name, route_data,
                    pad_positions_hash, data_size, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pcb_hash,
                    net_id,
                    net_name,
                    route_data,
                    pad_positions_hash,
                    data_size,
                    now,
                ),
            )

    def get_net_route(
        self,
        pcb_hash: str,
        net_id: int,
        pad_positions_hash: str,
    ) -> Route | None:
        """
        Get cached route for a single net if pad positions match.

        Args:
            pcb_hash: Hash of the PCB
            net_id: Net number
            pad_positions_hash: Current hash of pad positions

        Returns:
            Route if cached and pad positions match, None otherwise
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT route_data FROM partial_routes
                WHERE pcb_hash = ? AND net_id = ? AND pad_positions_hash = ?
                """,
                (pcb_hash, net_id, pad_positions_hash),
            )
            row = cursor.fetchone()

            if row is None:
                return None

            routes = self.deserialize_routes(row["route_data"])
            return routes[0] if routes else None

    def get_unchanged_net_routes(
        self,
        pcb_hash: str,
        net_pad_hashes: dict[int, str],
    ) -> dict[int, Route]:
        """
        Get cached routes for nets whose pad positions haven't changed.

        Args:
            pcb_hash: Hash of the PCB
            net_pad_hashes: Dict mapping net_id to current pad positions hash

        Returns:
            Dict mapping net_id to cached Route for unchanged nets
        """
        unchanged_routes = {}

        with self._connect() as conn:
            for net_id, pad_hash in net_pad_hashes.items():
                cursor = conn.execute(
                    """
                    SELECT route_data FROM partial_routes
                    WHERE pcb_hash = ? AND net_id = ? AND pad_positions_hash = ?
                    """,
                    (pcb_hash, net_id, pad_hash),
                )
                row = cursor.fetchone()

                if row is not None:
                    routes = self.deserialize_routes(row["route_data"])
                    if routes:
                        unchanged_routes[net_id] = routes[0]

        logger.info(
            f"Found {len(unchanged_routes)}/{len(net_pad_hashes)} unchanged net routes"
        )
        return unchanged_routes

    def _enforce_size_limit(self, new_data_size: int) -> None:
        """Evict oldest entries if cache exceeds size limit."""
        with self._connect() as conn:
            # Get current cache size
            cursor = conn.execute(
                "SELECT COALESCE(SUM(data_size), 0) FROM routing_results"
            )
            current_size = cursor.fetchone()[0]

            cursor = conn.execute(
                "SELECT COALESCE(SUM(data_size), 0) FROM partial_routes"
            )
            current_size += cursor.fetchone()[0]

            # Evict oldest entries if needed
            while current_size + new_data_size > self.max_size_bytes:
                # Find oldest routing result
                cursor = conn.execute(
                    """
                    SELECT cache_key, data_size FROM routing_results
                    ORDER BY last_accessed ASC LIMIT 1
                    """
                )
                row = cursor.fetchone()

                if row is None:
                    break

                conn.execute(
                    "DELETE FROM routing_results WHERE cache_key = ?",
                    (row["cache_key"],),
                )
                current_size -= row["data_size"]
                logger.debug(f"Evicted cache entry {row['cache_key']}")

    def clear(self) -> int:
        """
        Clear all cached routing results.

        Returns:
            Number of entries removed
        """
        with self._connect() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM routing_results")
            result_count = cursor.fetchone()[0]

            cursor = conn.execute("SELECT COUNT(*) FROM partial_routes")
            partial_count = cursor.fetchone()[0]

            conn.execute("DELETE FROM routing_results")
            conn.execute("DELETE FROM partial_routes")

            return result_count + partial_count

    def clear_expired(self) -> int:
        """
        Remove expired entries from cache.

        Returns:
            Number of entries removed
        """
        cutoff = (datetime.now() - self.ttl).isoformat()
        removed = 0

        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM routing_results WHERE created_at < ?",
                (cutoff,),
            )
            removed += cursor.rowcount

            cursor = conn.execute(
                "DELETE FROM partial_routes WHERE created_at < ?",
                (cutoff,),
            )
            removed += cursor.rowcount

        return removed

    def stats(self) -> dict:
        """
        Get cache statistics.

        Returns:
            Dict with cache stats
        """
        with self._connect() as conn:
            # Routing results stats
            result_count = conn.execute(
                "SELECT COUNT(*) FROM routing_results"
            ).fetchone()[0]

            result_size = conn.execute(
                "SELECT COALESCE(SUM(data_size), 0) FROM routing_results"
            ).fetchone()[0]

            # Partial routes stats
            partial_count = conn.execute(
                "SELECT COUNT(*) FROM partial_routes"
            ).fetchone()[0]

            partial_size = conn.execute(
                "SELECT COALESCE(SUM(data_size), 0) FROM partial_routes"
            ).fetchone()[0]

            # Hit rate (if tracked)
            cutoff = (datetime.now() - self.ttl).isoformat()
            valid_results = conn.execute(
                "SELECT COUNT(*) FROM routing_results WHERE created_at >= ?",
                (cutoff,),
            ).fetchone()[0]

            oldest = conn.execute(
                "SELECT MIN(created_at) FROM routing_results"
            ).fetchone()[0]

            newest = conn.execute(
                "SELECT MAX(created_at) FROM routing_results"
            ).fetchone()[0]

        total_size = result_size + partial_size
        return {
            "routing_results_count": result_count,
            "routing_results_size_bytes": result_size,
            "partial_routes_count": partial_count,
            "partial_routes_size_bytes": partial_size,
            "total_size_bytes": total_size,
            "total_size_mb": total_size / (1024 * 1024),
            "valid_results": valid_results,
            "expired_results": result_count - valid_results,
            "oldest": oldest,
            "newest": newest,
            "cache_dir": str(self.cache_dir),
            "ttl_days": self.ttl.days,
            "max_size_mb": self.max_size_bytes / (1024 * 1024),
        }

    def contains(self, key: CacheKey, ignore_expiry: bool = False) -> bool:
        """
        Check if routing result is cached.

        Args:
            key: Cache key to check
            ignore_expiry: If True, include expired entries

        Returns:
            True if cached (and not expired unless ignore_expiry)
        """
        with self._connect() as conn:
            if ignore_expiry:
                cursor = conn.execute(
                    "SELECT 1 FROM routing_results WHERE cache_key = ? AND version = ?",
                    (key.full_key, key.version),
                )
            else:
                cutoff = (datetime.now() - self.ttl).isoformat()
                cursor = conn.execute(
                    """
                    SELECT 1 FROM routing_results
                    WHERE cache_key = ? AND version = ? AND created_at >= ?
                    """,
                    (key.full_key, key.version, cutoff),
                )
            return cursor.fetchone() is not None


def compute_pad_positions_hash(pads: list[dict]) -> str:
    """Compute hash of pad positions for a net.

    Used to detect when a net's pads have moved, requiring re-routing.

    Args:
        pads: List of pad dicts with x, y positions

    Returns:
        SHA-256 hash of pad positions
    """
    # Sort pads by position for consistent hashing
    positions = sorted((p.get("x", 0), p.get("y", 0)) for p in pads)
    data = json.dumps(positions).encode("utf-8")
    return hashlib.sha256(data).hexdigest()

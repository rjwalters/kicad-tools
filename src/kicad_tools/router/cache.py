"""
SQLite cache for routing results.

Caches expensive routing computations to enable faster iteration during
PCB design. When a designer makes small changes to a PCB, unchanged nets
can reuse their cached routes instead of being re-routed from scratch.

Key features:
- Content-addressable caching using SHA-256 hashes
- Full routing result caching for exact PCB configurations
- Per-net partial route caching for incremental routing
- Sub-problem pattern caching for recurring pad geometries (Issue #2336)
- Automatic cache invalidation based on kicad-tools version
- Configurable TTL and max size limits
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .primitives import Pad, Route
    from .rules import DesignRules

logger = logging.getLogger(__name__)

# Version tag for cache invalidation when router algorithms change.
# Bump this constant whenever routing logic is modified to ensure stale
# cached results are not reused.  The value is included in every cache key
# so incrementing it automatically invalidates all existing entries.
CACHE_VERSION = "2.0.0"


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

        # Combine the package version with CACHE_VERSION so that either a
        # new release *or* a manual CACHE_VERSION bump invalidates the cache.
        pkg_version = _get_kicad_tools_version()
        version = f"{pkg_version}+cache.{CACHE_VERSION}"

        return cls(
            pcb_hash=pcb_hash,
            rules_hash=rules_hash,
            version=version,
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


@dataclass
class SubProblemSignature:
    """Position- and rotation-invariant signature for a routing sub-problem.

    Issue #2336: Captures the relative pad geometry and connectivity of a
    net, normalized so that the same physical pattern at different positions
    and rotations produces the same hash.  This allows solved routing
    sub-problems (e.g. bypass cap connections) to be reused across the board
    and even across different boards.

    Signature components:
    1. Relative pad positions (centroid at origin, first vector along +X)
    2. Pad dimensions and types (width, height, through-hole, drill)
    3. Layer assignment for each pad
    4. Design rules (clearance, trace width, via size)
    """

    signature_hash: str
    centroid_x: float
    centroid_y: float
    rotation_angle: float  # radians, used to un-rotate cached solution
    pad_count: int
    rules_hash: str

    @classmethod
    def compute(
        cls,
        pads: list[Pad],
        rules: DesignRules,
    ) -> SubProblemSignature:
        """Compute a position/rotation-invariant signature from pad geometry.

        Args:
            pads: List of Pad objects belonging to the net.
            rules: Design rules that affect routing solutions.

        Returns:
            SubProblemSignature with a deterministic hash.
        """
        if not pads:
            return cls(
                signature_hash="empty",
                centroid_x=0.0,
                centroid_y=0.0,
                rotation_angle=0.0,
                pad_count=0,
                rules_hash="",
            )

        # 1. Compute centroid
        cx = sum(p.x for p in pads) / len(pads)
        cy = sum(p.y for p in pads) / len(pads)

        # 2. Translate pads so centroid is at origin
        relative = [(p.x - cx, p.y - cy) for p in pads]

        # 3. Compute rotation angle: rotate so first pad (by angle from
        #    centroid) aligns with +X axis.  For single-pad nets or pads
        #    all at the centroid, rotation is 0.
        angles = [(math.atan2(ry, rx), i) for i, (rx, ry) in enumerate(relative)
                  if abs(rx) > 1e-6 or abs(ry) > 1e-6]

        if angles:
            angles.sort()
            rotation = angles[0][0]
        else:
            rotation = 0.0

        cos_r = math.cos(-rotation)
        sin_r = math.sin(-rotation)

        # 4. Rotate all relative positions and round for hashing stability.
        #    Adding 0.0 converts -0.0 to 0.0 so JSON serialization is stable.
        rotated = []
        for rx, ry in relative:
            nx = round(rx * cos_r - ry * sin_r, 4) + 0.0
            ny = round(rx * sin_r + ry * cos_r, 4) + 0.0
            rotated.append((nx, ny))

        # 5. Sort rotated positions for order independence, pairing with
        #    pad metadata
        pad_entries = []
        for i, (nx, ny) in enumerate(rotated):
            p = pads[i]
            pad_entries.append((
                nx, ny,
                round(p.width, 4),
                round(p.height, 4),
                p.through_hole,
                round(p.drill, 4),
                p.layer.value,
            ))
        pad_entries.sort()

        # 6. Build rules hash (only routing-relevant fields)
        rules_data = {
            "trace_width": rules.trace_width,
            "trace_clearance": rules.trace_clearance,
            "via_drill": rules.via_drill,
            "via_diameter": rules.via_diameter,
            "grid_resolution": rules.grid_resolution,
        }
        rules_json = json.dumps(rules_data, sort_keys=True)
        rules_hash = hashlib.sha256(rules_json.encode()).hexdigest()

        # 7. Combine everything into a signature hash
        sig_data = {
            "pads": pad_entries,
            "rules": rules_hash,
            "version": CACHE_VERSION,
        }
        sig_json = json.dumps(sig_data, sort_keys=True)
        signature_hash = hashlib.sha256(sig_json.encode()).hexdigest()

        return cls(
            signature_hash=signature_hash,
            centroid_x=cx,
            centroid_y=cy,
            rotation_angle=rotation,
            pad_count=len(pads),
            rules_hash=rules_hash,
        )


@dataclass
class CachedSubProblem:
    """A cached solution for a routing sub-problem.

    Stores the route segments/vias in centroid-relative, rotation-normalized
    coordinates.  On cache hit the caller applies an affine transform
    (rotate + translate) to place the solution at the current location.
    """

    signature_hash: str
    route_data: bytes  # Compressed JSON of relative route
    segment_count: int
    via_count: int
    hit_count: int
    created_at: datetime
    last_accessed: datetime


def transform_routes(
    routes: list[Route],
    dx: float,
    dy: float,
    angle: float,
    target_net: int,
    target_net_name: str,
) -> list[Route]:
    """Apply affine transform (rotate then translate) to route geometry.

    Args:
        routes: Routes in centroid-relative, rotation-normalized coordinates.
        dx: Translation in X (centroid of target pads).
        dy: Translation in Y (centroid of target pads).
        angle: Rotation angle in radians to apply.
        target_net: Net ID to assign to the transformed routes.
        target_net_name: Net name to assign.

    Returns:
        New list of Route objects with transformed coordinates.
    """
    from .layers import Layer
    from .primitives import Route as RouteClass
    from .primitives import Segment, Via

    cos_a = math.cos(angle)
    sin_a = math.sin(angle)

    def _transform(x: float, y: float) -> tuple[float, float]:
        rx = round(x * cos_a - y * sin_a + dx, 4)
        ry = round(x * sin_a + y * cos_a + dy, 4)
        return rx, ry

    transformed = []
    for route in routes:
        new_segs = []
        for seg in route.segments:
            x1, y1 = _transform(seg.x1, seg.y1)
            x2, y2 = _transform(seg.x2, seg.y2)
            new_segs.append(Segment(
                x1=x1, y1=y1, x2=x2, y2=y2,
                width=seg.width,
                layer=seg.layer,
                net=target_net,
                net_name=target_net_name,
            ))
        new_vias = []
        for via in route.vias:
            vx, vy = _transform(via.x, via.y)
            new_vias.append(Via(
                x=vx, y=vy,
                drill=via.drill,
                diameter=via.diameter,
                layers=via.layers,
                net=target_net,
                net_name=target_net_name,
            ))
        transformed.append(RouteClass(
            net=target_net,
            net_name=target_net_name,
            segments=new_segs,
            vias=new_vias,
        ))

    return transformed


def normalize_routes_to_origin(
    routes: list[Route],
    centroid_x: float,
    centroid_y: float,
    rotation_angle: float,
) -> list[Route]:
    """Transform routes into centroid-relative, rotation-normalized coordinates.

    This is the inverse of ``transform_routes``: first translate so that the
    centroid is at the origin, then rotate by ``-rotation_angle`` to align
    with the canonical orientation.

    Args:
        routes: Routes in world coordinates.
        centroid_x: X centroid of the pad configuration.
        centroid_y: Y centroid of the pad configuration.
        rotation_angle: Rotation angle that was applied to normalize pads.

    Returns:
        New list of Route objects in normalized coordinates.
    """
    from .primitives import Route as RouteClass
    from .primitives import Segment, Via

    # Inverse: translate by -centroid, then rotate by -angle
    cos_a = math.cos(-rotation_angle)
    sin_a = math.sin(-rotation_angle)

    def _inv_transform(x: float, y: float) -> tuple[float, float]:
        tx = x - centroid_x
        ty = y - centroid_y
        rx = round(tx * cos_a - ty * sin_a, 4)
        ry = round(tx * sin_a + ty * cos_a, 4)
        return rx, ry

    normalized = []
    for route in routes:
        new_segs = []
        for seg in route.segments:
            x1, y1 = _inv_transform(seg.x1, seg.y1)
            x2, y2 = _inv_transform(seg.x2, seg.y2)
            new_segs.append(Segment(
                x1=x1, y1=y1, x2=x2, y2=y2,
                width=seg.width,
                layer=seg.layer,
                net=0,
                net_name="",
            ))
        new_vias = []
        for via in route.vias:
            vx, vy = _inv_transform(via.x, via.y)
            new_vias.append(Via(
                x=vx, y=vy,
                drill=via.drill,
                diameter=via.diameter,
                layers=via.layers,
                net=0,
                net_name="",
            ))
        normalized.append(RouteClass(
            net=0,
            net_name="",
            segments=new_segs,
            vias=new_vias,
        ))

    return normalized


class RoutingCache:
    """
    SQLite-backed cache for routing results.

    Stores routing results locally to reduce computation time for
    iterative PCB design workflows. Supports both full routing result
    caching, per-net partial route caching for incremental updates,
    and sub-problem pattern caching for recurring pad geometries.

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

        # Sub-problem pattern reuse (Issue #2336)
        sig = SubProblemSignature.compute(pads, rules)
        cached_sub = cache.get_sub_problem(sig)
        if cached_sub:
            routes = cache.deserialize_routes(cached_sub.route_data)
            transformed = transform_routes(routes, sig.centroid_x, sig.centroid_y,
                                           sig.rotation_angle, net_id, net_name)
    """

    SCHEMA_VERSION = 3
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
                    version TEXT NOT NULL DEFAULT '',
                    data_size INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (pcb_hash, net_id)
                );

                CREATE TABLE IF NOT EXISTS sub_problem_solutions (
                    signature_hash TEXT PRIMARY KEY,
                    route_data BLOB NOT NULL,
                    segment_count INTEGER NOT NULL,
                    via_count INTEGER NOT NULL,
                    pad_count INTEGER NOT NULL,
                    rules_hash TEXT NOT NULL,
                    version TEXT NOT NULL,
                    hit_count INTEGER NOT NULL DEFAULT 0,
                    data_size INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    last_accessed TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_routing_results_pcb
                    ON routing_results(pcb_hash);
                CREATE INDEX IF NOT EXISTS idx_routing_results_accessed
                    ON routing_results(last_accessed);
                CREATE INDEX IF NOT EXISTS idx_partial_routes_hash
                    ON partial_routes(pad_positions_hash);
                CREATE INDEX IF NOT EXISTS idx_sub_problem_accessed
                    ON sub_problem_solutions(last_accessed);
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
        if from_version < 2:
            # Schema v2: add version column to partial_routes and drop stale
            # entries that were stored without version filtering.
            conn.execute("DELETE FROM partial_routes")
            conn.execute("ALTER TABLE partial_routes ADD COLUMN version TEXT NOT NULL DEFAULT ''")
            logger.info(
                "Migrated cache schema from v1 to v2: added version column to partial_routes"
            )

        if from_version < 3:
            # Schema v3 (Issue #2336): add sub_problem_solutions table for
            # pattern-level caching of recurring pad geometries.
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sub_problem_solutions (
                    signature_hash TEXT PRIMARY KEY,
                    route_data BLOB NOT NULL,
                    segment_count INTEGER NOT NULL,
                    via_count INTEGER NOT NULL,
                    pad_count INTEGER NOT NULL,
                    rules_hash TEXT NOT NULL,
                    version TEXT NOT NULL,
                    hit_count INTEGER NOT NULL DEFAULT 0,
                    data_size INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    last_accessed TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sub_problem_accessed
                    ON sub_problem_solutions(last_accessed);
            """)
            logger.info(
                "Migrated cache schema to v3: added sub_problem_solutions table"
            )

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
        pkg_version = _get_kicad_tools_version()
        version = f"{pkg_version}+cache.{CACHE_VERSION}"

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO partial_routes (
                    pcb_hash, net_id, net_name, route_data,
                    pad_positions_hash, version, data_size, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pcb_hash,
                    net_id,
                    net_name,
                    route_data,
                    pad_positions_hash,
                    version,
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
        pkg_version = _get_kicad_tools_version()
        version = f"{pkg_version}+cache.{CACHE_VERSION}"

        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT route_data FROM partial_routes
                WHERE pcb_hash = ? AND net_id = ? AND pad_positions_hash = ?
                      AND version = ?
                """,
                (pcb_hash, net_id, pad_positions_hash, version),
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
        pkg_version = _get_kicad_tools_version()
        version = f"{pkg_version}+cache.{CACHE_VERSION}"

        with self._connect() as conn:
            for net_id, pad_hash in net_pad_hashes.items():
                cursor = conn.execute(
                    """
                    SELECT route_data FROM partial_routes
                    WHERE pcb_hash = ? AND net_id = ? AND pad_positions_hash = ?
                          AND version = ?
                    """,
                    (pcb_hash, net_id, pad_hash, version),
                )
                row = cursor.fetchone()

                if row is not None:
                    routes = self.deserialize_routes(row["route_data"])
                    if routes:
                        unchanged_routes[net_id] = routes[0]

        logger.info(f"Found {len(unchanged_routes)}/{len(net_pad_hashes)} unchanged net routes")
        return unchanged_routes

    # ------------------------------------------------------------------
    # Sub-problem pattern cache (Issue #2336)
    # ------------------------------------------------------------------

    def put_sub_problem(
        self,
        signature: SubProblemSignature,
        routes: list[Route],
    ) -> None:
        """Store a solved routing sub-problem in the cache.

        The routes must already be in centroid-relative, rotation-normalized
        coordinates (use ``normalize_routes_to_origin`` before calling).

        Args:
            signature: Sub-problem signature computed from pad geometry.
            routes: Route objects in normalized coordinates.
        """
        route_data = self.serialize_routes(routes)
        data_size = len(route_data)
        now = datetime.now().isoformat()
        pkg_version = _get_kicad_tools_version()
        version = f"{pkg_version}+cache.{CACHE_VERSION}"

        seg_count = sum(len(r.segments) for r in routes)
        via_count = sum(len(r.vias) for r in routes)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sub_problem_solutions (
                    signature_hash, route_data, segment_count, via_count,
                    pad_count, rules_hash, version, hit_count,
                    data_size, created_at, last_accessed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    signature.signature_hash,
                    route_data,
                    seg_count,
                    via_count,
                    signature.pad_count,
                    signature.rules_hash,
                    version,
                    data_size,
                    now,
                    now,
                ),
            )

        logger.debug(
            "Cached sub-problem %s (%d segs, %d vias, %d bytes)",
            signature.signature_hash[:12],
            seg_count,
            via_count,
            data_size,
        )

    def get_sub_problem(
        self,
        signature: SubProblemSignature,
    ) -> CachedSubProblem | None:
        """Look up a cached sub-problem solution by geometry signature.

        Args:
            signature: Sub-problem signature to look up.

        Returns:
            CachedSubProblem if found and not expired, None otherwise.
        """
        pkg_version = _get_kicad_tools_version()
        version = f"{pkg_version}+cache.{CACHE_VERSION}"

        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM sub_problem_solutions
                WHERE signature_hash = ? AND version = ?
                """,
                (signature.signature_hash, version),
            )
            row = cursor.fetchone()

            if row is None:
                return None

            if self._is_expired(row["created_at"]):
                return None

            # Update access time and hit count
            conn.execute(
                """
                UPDATE sub_problem_solutions
                SET last_accessed = ?, hit_count = hit_count + 1
                WHERE signature_hash = ?
                """,
                (datetime.now().isoformat(), signature.signature_hash),
            )

            logger.debug(
                "Sub-problem cache hit: %s (hits: %d)",
                signature.signature_hash[:12],
                row["hit_count"] + 1,
            )
            return CachedSubProblem(
                signature_hash=row["signature_hash"],
                route_data=row["route_data"],
                segment_count=row["segment_count"],
                via_count=row["via_count"],
                hit_count=row["hit_count"] + 1,
                created_at=datetime.fromisoformat(row["created_at"]),
                last_accessed=datetime.fromisoformat(row["last_accessed"]),
            )

    def _enforce_size_limit(self, new_data_size: int) -> None:
        """Evict oldest entries if cache exceeds size limit."""
        with self._connect() as conn:
            # Get current cache size
            cursor = conn.execute("SELECT COALESCE(SUM(data_size), 0) FROM routing_results")
            current_size = cursor.fetchone()[0]

            cursor = conn.execute("SELECT COALESCE(SUM(data_size), 0) FROM partial_routes")
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

            cursor = conn.execute("SELECT COUNT(*) FROM sub_problem_solutions")
            sub_count = cursor.fetchone()[0]

            conn.execute("DELETE FROM routing_results")
            conn.execute("DELETE FROM partial_routes")
            conn.execute("DELETE FROM sub_problem_solutions")

            return result_count + partial_count + sub_count

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

            cursor = conn.execute(
                "DELETE FROM sub_problem_solutions WHERE created_at < ?",
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
            result_count = conn.execute("SELECT COUNT(*) FROM routing_results").fetchone()[0]

            result_size = conn.execute(
                "SELECT COALESCE(SUM(data_size), 0) FROM routing_results"
            ).fetchone()[0]

            # Partial routes stats
            partial_count = conn.execute("SELECT COUNT(*) FROM partial_routes").fetchone()[0]

            partial_size = conn.execute(
                "SELECT COALESCE(SUM(data_size), 0) FROM partial_routes"
            ).fetchone()[0]

            # Hit rate (if tracked)
            cutoff = (datetime.now() - self.ttl).isoformat()
            valid_results = conn.execute(
                "SELECT COUNT(*) FROM routing_results WHERE created_at >= ?",
                (cutoff,),
            ).fetchone()[0]

            oldest = conn.execute("SELECT MIN(created_at) FROM routing_results").fetchone()[0]

            newest = conn.execute("SELECT MAX(created_at) FROM routing_results").fetchone()[0]

            # Sub-problem stats (Issue #2336)
            sub_count = conn.execute(
                "SELECT COUNT(*) FROM sub_problem_solutions"
            ).fetchone()[0]

            sub_size = conn.execute(
                "SELECT COALESCE(SUM(data_size), 0) FROM sub_problem_solutions"
            ).fetchone()[0]

            sub_total_hits = conn.execute(
                "SELECT COALESCE(SUM(hit_count), 0) FROM sub_problem_solutions"
            ).fetchone()[0]

        total_size = result_size + partial_size + sub_size
        return {
            "routing_results_count": result_count,
            "routing_results_size_bytes": result_size,
            "partial_routes_count": partial_count,
            "partial_routes_size_bytes": partial_size,
            "sub_problem_count": sub_count,
            "sub_problem_size_bytes": sub_size,
            "sub_problem_total_hits": sub_total_hits,
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

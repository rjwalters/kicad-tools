"""Tests for routing result cache."""

import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from kicad_tools.router import (
    CacheKey,
    DesignRules,
    Route,
    RoutingCache,
    Segment,
    Via,
    compute_pad_positions_hash,
)
from kicad_tools.router.cache import CACHE_VERSION
from kicad_tools.router.layers import Layer


class TestCacheKey:
    """Tests for cache key computation."""

    def test_compute_from_string(self):
        """Test computing cache key from string content."""
        pcb_content = "(kicad_pcb (test content))"
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            grid_resolution=0.1,
        )

        key = CacheKey.compute(pcb_content, rules, 0.1)

        assert key.pcb_hash is not None
        assert len(key.pcb_hash) == 64  # SHA-256 hex
        assert key.rules_hash is not None
        assert len(key.rules_hash) == 64
        assert key.version is not None

    def test_compute_from_bytes(self):
        """Test computing cache key from bytes content."""
        pcb_content = b"(kicad_pcb (test content))"
        rules = DesignRules()

        key = CacheKey.compute(pcb_content, rules, 0.25)

        assert key.pcb_hash is not None
        assert key.rules_hash is not None

    def test_same_input_same_key(self):
        """Test that same inputs produce same key."""
        pcb_content = "(kicad_pcb (test))"
        rules = DesignRules(trace_width=0.2, trace_clearance=0.15)

        key1 = CacheKey.compute(pcb_content, rules, 0.1)
        key2 = CacheKey.compute(pcb_content, rules, 0.1)

        assert key1.full_key == key2.full_key

    def test_different_content_different_key(self):
        """Test that different content produces different key."""
        rules = DesignRules()

        key1 = CacheKey.compute("content1", rules, 0.1)
        key2 = CacheKey.compute("content2", rules, 0.1)

        assert key1.pcb_hash != key2.pcb_hash
        assert key1.full_key != key2.full_key

    def test_different_rules_different_key(self):
        """Test that different rules produce different key."""
        pcb_content = "(kicad_pcb)"
        rules1 = DesignRules(trace_width=0.2)
        rules2 = DesignRules(trace_width=0.3)

        key1 = CacheKey.compute(pcb_content, rules1, 0.1)
        key2 = CacheKey.compute(pcb_content, rules2, 0.1)

        assert key1.rules_hash != key2.rules_hash
        assert key1.full_key != key2.full_key

    def test_different_grid_different_key(self):
        """Test that different grid resolution produces different key."""
        pcb_content = "(kicad_pcb)"
        rules = DesignRules()

        key1 = CacheKey.compute(pcb_content, rules, 0.1)
        key2 = CacheKey.compute(pcb_content, rules, 0.25)

        assert key1.rules_hash != key2.rules_hash

    def test_full_key_format(self):
        """Test full_key property format."""
        key = CacheKey(
            pcb_hash="a" * 64,
            rules_hash="b" * 64,
            version="1.0.0+cache.2.0.0",
        )

        assert ":" in key.full_key
        assert key.full_key.count(":") == 2
        assert "1.0.0+cache.2.0.0" in key.full_key

    def test_cache_version_included_in_computed_key(self):
        """Test that CACHE_VERSION is included in the computed version field."""
        pcb_content = "(kicad_pcb (test))"
        rules = DesignRules()

        key = CacheKey.compute(pcb_content, rules, 0.1)

        assert f"+cache.{CACHE_VERSION}" in key.version

    def test_cache_version_change_invalidates_key(self):
        """Test that changing CACHE_VERSION produces a different cache key."""
        pcb_content = "(kicad_pcb (test))"
        rules = DesignRules()

        key1 = CacheKey.compute(pcb_content, rules, 0.1)

        with mock.patch("kicad_tools.router.cache.CACHE_VERSION", "99.0.0"):
            key2 = CacheKey.compute(pcb_content, rules, 0.1)

        assert key1.version != key2.version
        assert key1.full_key != key2.full_key


class TestRoutingCache:
    """Tests for RoutingCache class."""

    @pytest.fixture
    def temp_cache(self, tmp_path):
        """Create a temporary cache for testing."""
        cache_dir = tmp_path / "routing_cache"
        return RoutingCache(cache_dir=cache_dir, ttl_days=30)

    @pytest.fixture
    def sample_routes(self):
        """Create sample routes for testing."""
        return [
            Route(
                net=1,
                net_name="NET1",
                segments=[
                    Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.F_CU, net=1),
                    Segment(x1=10, y1=0, x2=10, y2=10, width=0.2, layer=Layer.F_CU, net=1),
                ],
                vias=[],
            ),
            Route(
                net=2,
                net_name="NET2",
                segments=[
                    Segment(x1=5, y1=5, x2=15, y2=5, width=0.2, layer=Layer.B_CU, net=2),
                ],
                vias=[
                    Via(x=5, y=5, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=2),
                ],
            ),
        ]

    @pytest.fixture
    def sample_key(self):
        """Create sample cache key."""
        return CacheKey.compute(
            "(kicad_pcb (test))",
            DesignRules(),
            0.1,
        )

    def test_init_creates_database(self, temp_cache):
        """Test that initialization creates database."""
        assert temp_cache.db_path.exists()

    def test_put_and_get(self, temp_cache, sample_routes, sample_key):
        """Test storing and retrieving routing results."""
        statistics = {
            "routes": 2,
            "segments": 3,
            "vias": 1,
            "failures": 0,
        }

        # Store
        temp_cache.put(sample_key, sample_routes, statistics, compute_time_ms=1000)

        # Retrieve
        result = temp_cache.get(sample_key)

        assert result is not None
        assert result.success_count == 2
        assert result.total_segments == 3
        assert result.total_vias == 1
        assert result.compute_time_ms == 1000

    def test_get_cache_miss(self, temp_cache, sample_key):
        """Test cache miss returns None."""
        result = temp_cache.get(sample_key)
        assert result is None

    def test_deserialize_routes(self, temp_cache, sample_routes, sample_key):
        """Test that routes can be deserialized correctly."""
        statistics = {"routes": 2, "segments": 3, "vias": 1}
        temp_cache.put(sample_key, sample_routes, statistics)

        result = temp_cache.get(sample_key)
        routes = temp_cache.deserialize_routes(result.routes_data)

        assert len(routes) == 2
        assert routes[0].net == 1
        assert routes[0].net_name == "NET1"
        assert len(routes[0].segments) == 2
        assert routes[1].net == 2
        assert len(routes[1].vias) == 1
        assert routes[1].vias[0].drill == 0.3

    def test_serialize_preserves_data(self, temp_cache, sample_routes):
        """Test that serialization preserves all route data."""
        data = temp_cache.serialize_routes(sample_routes)
        routes = temp_cache.deserialize_routes(data)

        # Check first route
        assert routes[0].net == sample_routes[0].net
        assert routes[0].net_name == sample_routes[0].net_name
        assert len(routes[0].segments) == len(sample_routes[0].segments)
        seg = routes[0].segments[0]
        orig_seg = sample_routes[0].segments[0]
        assert seg.x1 == orig_seg.x1
        assert seg.y1 == orig_seg.y1
        assert seg.x2 == orig_seg.x2
        assert seg.y2 == orig_seg.y2
        assert seg.width == orig_seg.width
        assert seg.layer == orig_seg.layer

        # Check second route with via
        via = routes[1].vias[0]
        orig_via = sample_routes[1].vias[0]
        assert via.x == orig_via.x
        assert via.y == orig_via.y
        assert via.drill == orig_via.drill
        assert via.diameter == orig_via.diameter
        assert via.layers == orig_via.layers

    def test_contains(self, temp_cache, sample_routes, sample_key):
        """Test contains method."""
        assert not temp_cache.contains(sample_key)

        temp_cache.put(sample_key, sample_routes, {"routes": 2})

        assert temp_cache.contains(sample_key)

    def test_clear(self, temp_cache, sample_routes, sample_key):
        """Test clearing cache."""
        temp_cache.put(sample_key, sample_routes, {"routes": 2})
        assert temp_cache.contains(sample_key)

        count = temp_cache.clear()

        assert count == 1
        assert not temp_cache.contains(sample_key)

    def test_stats(self, temp_cache, sample_routes, sample_key):
        """Test cache statistics."""
        temp_cache.put(sample_key, sample_routes, {"routes": 2})

        stats = temp_cache.stats()

        assert stats["routing_results_count"] == 1
        assert stats["total_size_bytes"] > 0
        assert stats["valid_results"] == 1
        assert stats["ttl_days"] == 30

    def test_version_mismatch(self, temp_cache, sample_routes):
        """Test that version mismatch causes cache miss."""
        # Store with one version
        key1 = CacheKey(
            pcb_hash="a" * 64,
            rules_hash="b" * 64,
            version="1.0.0",
        )
        temp_cache.put(key1, sample_routes, {"routes": 2})

        # Try to retrieve with different version
        key2 = CacheKey(
            pcb_hash="a" * 64,
            rules_hash="b" * 64,
            version="2.0.0",
        )
        result = temp_cache.get(key2)

        assert result is None


class TestPartialRouteCache:
    """Tests for per-net partial route caching."""

    @pytest.fixture
    def temp_cache(self, tmp_path):
        """Create a temporary cache for testing."""
        cache_dir = tmp_path / "routing_cache"
        return RoutingCache(cache_dir=cache_dir)

    @pytest.fixture
    def sample_route(self):
        """Create a sample route."""
        return Route(
            net=1,
            net_name="NET1",
            segments=[
                Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.F_CU, net=1),
            ],
            vias=[],
        )

    def test_put_and_get_net_route(self, temp_cache, sample_route):
        """Test storing and retrieving single net route."""
        pcb_hash = "abc123" * 10
        pad_hash = "def456" * 10

        temp_cache.put_net_route(
            pcb_hash=pcb_hash,
            net_id=1,
            net_name="NET1",
            route=sample_route,
            pad_positions_hash=pad_hash,
        )

        result = temp_cache.get_net_route(pcb_hash, 1, pad_hash)

        assert result is not None
        assert result.net == 1
        assert result.net_name == "NET1"
        assert len(result.segments) == 1

    def test_get_net_route_pad_mismatch(self, temp_cache, sample_route):
        """Test that pad position mismatch causes cache miss."""
        pcb_hash = "abc123" * 10
        pad_hash1 = "def456" * 10
        pad_hash2 = "ghi789" * 10

        temp_cache.put_net_route(
            pcb_hash=pcb_hash,
            net_id=1,
            net_name="NET1",
            route=sample_route,
            pad_positions_hash=pad_hash1,
        )

        result = temp_cache.get_net_route(pcb_hash, 1, pad_hash2)

        assert result is None

    def test_get_unchanged_net_routes(self, temp_cache, sample_route):
        """Test getting routes for unchanged nets."""
        pcb_hash = "abc123" * 10
        pad_hash1 = "hash1" * 13
        pad_hash2 = "hash2" * 13

        # Store routes for two nets
        route2 = Route(
            net=2,
            net_name="NET2",
            segments=[
                Segment(x1=5, y1=5, x2=15, y2=5, width=0.2, layer=Layer.B_CU, net=2),
            ],
            vias=[],
        )

        temp_cache.put_net_route(pcb_hash, 1, "NET1", sample_route, pad_hash1)
        temp_cache.put_net_route(pcb_hash, 2, "NET2", route2, pad_hash2)

        # Request with matching hashes
        net_pad_hashes = {
            1: pad_hash1,  # Unchanged
            2: "different",  # Changed
            3: "new_net",  # New net
        }

        unchanged = temp_cache.get_unchanged_net_routes(pcb_hash, net_pad_hashes)

        assert len(unchanged) == 1
        assert 1 in unchanged
        assert 2 not in unchanged
        assert 3 not in unchanged

    def test_partial_route_version_mismatch(self, temp_cache, sample_route):
        """Test that partial routes with a different CACHE_VERSION are not returned."""
        pcb_hash = "abc123" * 10
        pad_hash = "def456" * 10

        # Store a partial route with current version
        temp_cache.put_net_route(pcb_hash, 1, "NET1", sample_route, pad_hash)

        # Retrieve with a different CACHE_VERSION -- should miss
        with mock.patch("kicad_tools.router.cache.CACHE_VERSION", "99.0.0"):
            result = temp_cache.get_net_route(pcb_hash, 1, pad_hash)

        assert result is None

    def test_unchanged_net_routes_version_mismatch(self, temp_cache, sample_route):
        """Test that get_unchanged_net_routes filters by version."""
        pcb_hash = "abc123" * 10
        pad_hash = "hash1" * 13

        temp_cache.put_net_route(pcb_hash, 1, "NET1", sample_route, pad_hash)

        # With a different CACHE_VERSION the route should not be returned
        with mock.patch("kicad_tools.router.cache.CACHE_VERSION", "99.0.0"):
            unchanged = temp_cache.get_unchanged_net_routes(pcb_hash, {1: pad_hash})

        assert len(unchanged) == 0


class TestComputePadPositionsHash:
    """Tests for pad positions hash computation."""

    def test_same_pads_same_hash(self):
        """Test that same pads produce same hash."""
        pads = [
            {"x": 0, "y": 0},
            {"x": 10, "y": 5},
        ]

        hash1 = compute_pad_positions_hash(pads)
        hash2 = compute_pad_positions_hash(pads)

        assert hash1 == hash2

    def test_different_order_same_hash(self):
        """Test that pad order doesn't affect hash (sorted internally)."""
        pads1 = [
            {"x": 0, "y": 0},
            {"x": 10, "y": 5},
        ]
        pads2 = [
            {"x": 10, "y": 5},
            {"x": 0, "y": 0},
        ]

        hash1 = compute_pad_positions_hash(pads1)
        hash2 = compute_pad_positions_hash(pads2)

        assert hash1 == hash2

    def test_different_positions_different_hash(self):
        """Test that different positions produce different hash."""
        pads1 = [{"x": 0, "y": 0}]
        pads2 = [{"x": 1, "y": 0}]

        hash1 = compute_pad_positions_hash(pads1)
        hash2 = compute_pad_positions_hash(pads2)

        assert hash1 != hash2

    def test_empty_pads(self):
        """Test hash of empty pad list."""
        hash1 = compute_pad_positions_hash([])
        assert hash1 is not None
        assert len(hash1) == 64  # SHA-256 hex


class TestCacheSizeLimits:
    """Tests for cache size limit enforcement."""

    @pytest.fixture
    def small_cache(self, tmp_path):
        """Create a cache with small size limit."""
        cache_dir = tmp_path / "small_cache"
        # 1KB max size
        return RoutingCache(cache_dir=cache_dir, max_size_mb=0.001)

    def test_size_limit_eviction(self, small_cache):
        """Test that old entries are evicted when size limit exceeded."""
        rules = DesignRules()

        # Add several entries to exceed size limit
        # Create large routes with many segments to ensure we exceed 1KB per entry
        for i in range(10):
            key = CacheKey.compute(f"content{i}" * 100, rules, 0.1)
            # Create many segments to make a larger entry
            segments = [
                Segment(
                    x1=j,
                    y1=j,
                    x2=j + 10,
                    y2=j,
                    width=0.2,
                    layer=Layer.F_CU,
                    net=i,
                    net_name=f"NET{i}" * 10,  # Longer net name
                )
                for j in range(100)  # 100 segments per route
            ]
            routes = [
                Route(
                    net=i,
                    net_name=f"NET{i}" * 50,  # Long net name
                    segments=segments,
                    vias=[],
                )
            ]
            small_cache.put(key, routes, {"routes": 1})

        # Cache should have evicted some entries (1KB is very small)
        stats = small_cache.stats()
        # With a 1KB limit and ~3-5KB entries, we should have evicted most
        assert stats["routing_results_count"] < 10


class TestCacheExpiry:
    """Tests for cache entry expiration."""

    @pytest.fixture
    def short_ttl_cache(self, tmp_path):
        """Create a cache with short TTL for testing expiry."""
        cache_dir = tmp_path / "short_ttl_cache"
        # Use TTL of 0 days (always expired)
        return RoutingCache(cache_dir=cache_dir, ttl_days=0)

    def test_expired_entry_not_returned(self, short_ttl_cache):
        """Test that expired entries are not returned by default."""
        key = CacheKey.compute("content", DesignRules(), 0.1)
        routes = [Route(net=1, net_name="NET1", segments=[], vias=[])]

        short_ttl_cache.put(key, routes, {"routes": 1})

        # With 0-day TTL, entry is immediately expired
        result = short_ttl_cache.get(key)
        assert result is None

    def test_expired_entry_returned_with_flag(self, short_ttl_cache):
        """Test that expired entries can be retrieved with ignore_expiry."""
        key = CacheKey.compute("content", DesignRules(), 0.1)
        routes = [Route(net=1, net_name="NET1", segments=[], vias=[])]

        short_ttl_cache.put(key, routes, {"routes": 1})

        result = short_ttl_cache.get(key, ignore_expiry=True)
        assert result is not None

    def test_clear_expired(self, short_ttl_cache):
        """Test clearing expired entries."""
        key = CacheKey.compute("content", DesignRules(), 0.1)
        routes = [Route(net=1, net_name="NET1", segments=[], vias=[])]

        short_ttl_cache.put(key, routes, {"routes": 1})

        # Clear expired
        count = short_ttl_cache.clear_expired()
        assert count == 1

        # Entry should be gone
        result = short_ttl_cache.get(key, ignore_expiry=True)
        assert result is None


class TestSchemaMigration:
    """Tests for database schema migration from v1 to v2."""

    def _create_v1_database(self, db_path: Path) -> None:
        """Manually create a v1 schema database with sample data."""
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE routing_results (
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

            CREATE TABLE partial_routes (
                pcb_hash TEXT NOT NULL,
                net_id INTEGER NOT NULL,
                net_name TEXT NOT NULL,
                route_data BLOB NOT NULL,
                pad_positions_hash TEXT NOT NULL,
                data_size INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (pcb_hash, net_id)
            );

            INSERT INTO meta (key, value) VALUES ('schema_version', '1');
            INSERT INTO meta (key, value) VALUES ('cache_version', '1.0.0');

            INSERT INTO partial_routes (pcb_hash, net_id, net_name, route_data,
                pad_positions_hash, data_size, created_at)
            VALUES ('old_hash', 1, 'NET1', X'00', 'pad_hash', 1, '2024-01-01T00:00:00');
        """)
        conn.commit()
        conn.close()

    def test_migration_v1_to_v2_drops_old_partial_routes(self, tmp_path):
        """Test that migration from v1 drops stale partial routes."""
        cache_dir = tmp_path / "migrate_cache"
        cache_dir.mkdir()
        db_path = cache_dir / "routing.db"
        self._create_v1_database(db_path)

        # Opening the cache should trigger migration
        RoutingCache(cache_dir=cache_dir)

        # Old partial routes should be gone
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM partial_routes").fetchone()[0]
        conn.close()
        assert count == 0

    def test_migration_v1_to_v2_adds_version_column(self, tmp_path):
        """Test that migration adds a version column to partial_routes."""
        cache_dir = tmp_path / "migrate_cache"
        cache_dir.mkdir()
        db_path = cache_dir / "routing.db"
        self._create_v1_database(db_path)

        cache = RoutingCache(cache_dir=cache_dir)

        # Verify version column exists by inserting a partial route
        route = Route(
            net=1,
            net_name="NET1",
            segments=[Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.F_CU, net=1)],
            vias=[],
        )
        cache.put_net_route("test_hash", 1, "NET1", route, "pad_hash")

        # Verify the version was stored
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT version FROM partial_routes WHERE net_id = 1").fetchone()
        conn.close()
        assert row is not None
        assert f"+cache.{CACHE_VERSION}" in row["version"]

    def test_migration_updates_schema_version(self, tmp_path):
        """Test that migration updates schema_version in meta table."""
        cache_dir = tmp_path / "migrate_cache"
        cache_dir.mkdir()
        db_path = cache_dir / "routing.db"
        self._create_v1_database(db_path)

        RoutingCache(cache_dir=cache_dir)

        conn = sqlite3.connect(str(db_path))
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
        conn.close()
        assert version == "2"

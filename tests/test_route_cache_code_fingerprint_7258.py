"""The routing cache is keyed on the code that routes (Issue #7258)."""

from __future__ import annotations

import pytest

from kicad_tools.router import CacheKey, DesignRules, Route, RoutingCache, Segment
from kicad_tools.router import cache as cache_mod
from kicad_tools.router.cache import _cache_version_tag, _hash_source_tree
from kicad_tools.router.layers import Layer


@pytest.fixture
def cache(tmp_path) -> RoutingCache:
    return RoutingCache(cache_dir=tmp_path / "routing_cache", ttl_days=30)


@pytest.fixture
def route() -> Route:
    return Route(
        net=1,
        net_name="NET1",
        segments=[Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.F_CU, net=1)],
        vias=[],
    )


def _set_fingerprint(monkeypatch, value: str) -> None:
    monkeypatch.setattr(cache_mod, "_code_fingerprint", lambda: value)


def test_code_change_misses_and_restore_hits(monkeypatch, cache, route):
    pcb, rules = "(kicad_pcb (net 1 NET1))", DesignRules()
    _set_fingerprint(monkeypatch, "aaaa")
    cache.put(CacheKey.compute(pcb, rules, 0.1), [route], {"routes": 1})
    assert cache.get(CacheKey.compute(pcb, rules, 0.1)) is not None

    _set_fingerprint(monkeypatch, "bbbb")
    assert cache.get(CacheKey.compute(pcb, rules, 0.1)) is None

    _set_fingerprint(monkeypatch, "aaaa")
    assert cache.get(CacheKey.compute(pcb, rules, 0.1)) is not None


def test_tag_carries_code_segment(monkeypatch):
    _set_fingerprint(monkeypatch, "abc123")
    assert "+code.abc123+native." in _cache_version_tag()


def test_fingerprint_computed_once(monkeypatch):
    calls = []
    cache_mod._code_fingerprint.cache_clear()
    monkeypatch.setattr(
        cache_mod, "_hash_source_tree", lambda root, ext=None: calls.append(1) or "x"
    )
    try:
        for _ in range(3):
            _cache_version_tag()
        assert len(calls) == 1
    finally:
        cache_mod._code_fingerprint.cache_clear()


def test_hash_source_tree_tracks_one_byte(tmp_path):
    (tmp_path / "sub").mkdir()
    mod = tmp_path / "sub" / "m.py"
    mod.write_text("x = 1\n")
    (tmp_path / "other.py").write_text("y = 2\n")
    before = _hash_source_tree(tmp_path)
    assert _hash_source_tree(tmp_path) == before
    mod.write_text("x = 2\n")
    assert _hash_source_tree(tmp_path) != before


def test_hash_source_tree_includes_extension(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    so = tmp_path.parent / f"{tmp_path.name}.so"
    so.write_bytes(b"one")
    first = _hash_source_tree(tmp_path, so)
    so.write_bytes(b"two")
    assert _hash_source_tree(tmp_path, so) != first

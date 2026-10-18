"""The routing cache is keyed on the loaded native router build (Issue #7014).

A C++ router rebuild that bumps ``BUILD_VERSION`` changes routed copper
without touching any Python-side input.  Before #7014 the cache key held only
the package version and ``CACHE_VERSION``, so a hit replayed copper produced
by the previous router.  These tests simulate a rebuild by monkeypatching the
version source ``kct build-native --check`` reads (``get_backend_info``).
"""

from __future__ import annotations

import pytest

from kicad_tools.router import CacheKey, DesignRules, Route, RoutingCache, Segment, cpp_backend
from kicad_tools.router.cache import SubProblemSignature, _cache_version_tag
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad


def _set_native_build(monkeypatch: pytest.MonkeyPatch, build: int | None) -> None:
    """Pretend the loaded C++ router reports ``build`` (``None`` = unavailable)."""

    def fake_info() -> dict:
        if build is None:
            return {"available": False, "build_version": None}
        return {"available": True, "build_version": build}

    monkeypatch.setattr(cpp_backend, "get_backend_info", fake_info)


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


def test_full_route_written_under_build_n_misses_under_n_plus_1(monkeypatch, cache, route):
    pcb, rules = "(kicad_pcb (net 1 NET1))", DesignRules()

    _set_native_build(monkeypatch, 59)
    key_n = CacheKey.compute(pcb, rules, 0.1)
    cache.put(key_n, [route], {"routes": 1})
    assert cache.get(CacheKey.compute(pcb, rules, 0.1)) is not None, "same build must hit"

    _set_native_build(monkeypatch, 60)
    key_n1 = CacheKey.compute(pcb, rules, 0.1)
    assert key_n1.full_key != key_n.full_key
    assert cache.get(key_n1) is None


def test_partial_route_written_under_build_n_misses_under_n_plus_1(monkeypatch, cache, route):
    pcb_hash, pad_hash = "a" * 64, "b" * 64

    _set_native_build(monkeypatch, 59)
    cache.put_net_route(pcb_hash, 1, "NET1", route, pad_hash)
    assert cache.get_net_route(pcb_hash, 1, pad_hash) is not None

    _set_native_build(monkeypatch, 60)
    assert cache.get_net_route(pcb_hash, 1, pad_hash) is None
    assert cache.get_unchanged_net_routes(pcb_hash, {1: pad_hash}) == {}


def test_sub_problem_written_under_build_n_misses_under_n_plus_1(monkeypatch, cache, route):
    pads = [
        Pad(x=0, y=0, width=0.6, height=0.6, net=1, net_name="N", layer=Layer.F_CU),
        Pad(x=10, y=0, width=0.6, height=0.6, net=1, net_name="N", layer=Layer.F_CU),
    ]
    sig = SubProblemSignature.compute(pads, DesignRules())

    _set_native_build(monkeypatch, 59)
    cache.put_sub_problem(sig, [route])
    assert cache.get_sub_problem(sig) is not None

    _set_native_build(monkeypatch, 60)
    assert cache.get_sub_problem(sig) is None


def test_python_fallback_does_not_share_an_entry_with_a_cpp_route(monkeypatch):
    """A missing/stale extension routes in Python: different copper, different key."""
    _set_native_build(monkeypatch, 59)
    cpp_tag = _cache_version_tag()
    _set_native_build(monkeypatch, None)
    python_tag = _cache_version_tag()

    assert cpp_tag.endswith("+native.59")
    assert python_tag.endswith("+native.none")


def test_tag_reports_the_build_kct_build_native_check_reports():
    """Unpatched, the tag follows the live ``get_backend_info`` answer."""
    info = cpp_backend.get_backend_info()
    expected = str(info["build_version"]) if info.get("available") else "none"
    assert _cache_version_tag().endswith(f"+native.{expected}")

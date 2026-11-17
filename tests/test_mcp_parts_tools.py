"""Tests for the parts_search / parts_lookup MCP tools (Issue #7993).

The LCSC client is stubbed at its network/catalog boundary, so no test touches
the network.
"""

from __future__ import annotations

import pytest

from kicad_tools.mcp.tools.parts import parts_lookup, parts_search
from kicad_tools.mcp.tools.registry import TOOL_REGISTRY
from kicad_tools.parts import LCSCClient, LCSCUnavailableError, Part
from kicad_tools.parts.models import LookupResult, PartPrice, SearchResult

PART = Part(
    lcsc_part="C25804",
    mfr_part="0402WGF1002TCE",
    package="0402",
    stock=100,
    prices=[PartPrice(quantity=1, unit_price=0.001)],
)


def _patch(monkeypatch, *, search=None, lookup=None):
    if search is not None:
        monkeypatch.setattr(LCSCClient, "search", search)
    if lookup is not None:
        monkeypatch.setattr(LCSCClient, "lookup_result", lookup)


def _raise(exc):
    def fn(self, *args, **kwargs):
        raise exc

    return fn


def test_tools_registered():
    for name, required in (("parts_search", ["query"]), ("parts_lookup", ["lcsc_part"])):
        assert TOOL_REGISTRY[name].parameters["required"] == required


def test_search_offline_fallback(monkeypatch):
    seen = {}

    def fake(self, query, **kw):
        seen.update(kw, query=query)
        return SearchResult(
            query=query,
            parts=[PART],
            total_count=1,
            source="offline",
            coverage="offline",
            diagnostics=["live: unreachable"],
        )

    _patch(monkeypatch, search=fake)
    out = parts_search("10k 0402", package="0402", basic_only=True, page_size=5)
    assert "error" not in out
    assert out["coverage"] == "offline" and out["source"] == "offline"
    assert out["parts"][0]["lcsc_part"] == "C25804"
    assert out["diagnostics"] == ["live: unreachable"]
    assert seen["package"] == "0402" and seen["basic_only"] is True and seen["page_size"] == 5


def test_search_unavailable_is_error_not_empty(monkeypatch):
    _patch(monkeypatch, search=_raise(LCSCUnavailableError("no live, no catalog")))
    out = parts_search("10k")
    assert "no live, no catalog" in out["error"]
    assert out["status"] == "unavailable"
    assert "parts" not in out


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": ""},
        {"query": "   "},
        {"query": None},
        {"query": "x", "page": 0},
        {"query": "x", "page_size": 101},
        {"query": "x", "page_size": 0},
    ],
)
def test_search_invalid_input(kwargs, monkeypatch):
    _patch(monkeypatch, search=_raise(AssertionError("client must not be called")))
    assert "error" in parts_search(**kwargs)


def test_lookup_offline_found(monkeypatch):
    _patch(
        monkeypatch,
        lookup=lambda self, p, **kw: LookupResult(PART, "offline", "found", ["live: 403"]),
    )
    out = parts_lookup("25804")
    assert "error" not in out
    assert out["status"] == "found" and out["source"] == "offline"
    assert out["part"]["prices"] == [{"quantity": 1, "unit_price": 0.001}]


def test_lookup_verified_not_found_has_no_error(monkeypatch):
    _patch(monkeypatch, lookup=lambda self, p, **kw: LookupResult(None, "live", "not_found", []))
    out = parts_lookup("C1")
    assert out["status"] == "not_found" and out["part"] is None and "error" not in out


@pytest.mark.parametrize("status", ["offline_miss", "unavailable"])
def test_lookup_unverified_miss_is_error(monkeypatch, status):
    _patch(monkeypatch, lookup=lambda self, p, **kw: LookupResult(None, "none", status, ["x"]))
    out = parts_lookup("C1")
    assert out["status"] == status and "error" in out and out["part"] is None


def test_lookup_backend_unavailable_exception(monkeypatch):
    _patch(monkeypatch, lookup=_raise(LCSCUnavailableError("403 circuit open")))
    out = parts_lookup("C1")
    assert "403 circuit open" in out["error"] and out["status"] == "unavailable"


@pytest.mark.parametrize("bad", ["", "abc", "C12x", "C", None, 123])
def test_lookup_invalid_id(bad, monkeypatch):
    _patch(monkeypatch, lookup=_raise(AssertionError("client must not be called")))
    assert "error" in parts_lookup(bad)
